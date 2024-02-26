import asyncio
import json
import os

import re
from logging import getLogger
from typing import List, Optional, Any
from multiprocessing import Pool
import asyncpg

from pg_anon.common import (
    PgAnonResult,
    ResultCode,
    ScanMode,
    exception_helper,
    setof_to_list,
)


logger = getLogger(__name__)

SENS_PG_TYPES = ["text", "integer", "bigint", "character", "json"]


class TaggedFields:
    def __init__(
        self, nspname: str, relname: str, column_name: str, column_comment: str
    ):
        self.nspname = nspname
        self.relname = relname
        self.column_name = column_name
        self.column_comment = column_comment


class FieldInfo:
    query: Optional[str] = None
    sensitive: Optional[bool] = None
    # Todo: save row data as set() it may be faster, at least will remove duplicated records
    row_data: List[Any] = None

    def __init__(
        self,
        nspname: str,
        relname: str,
        column_name: str,
        type: str,
        oid: int,
        attnum: int,
        obj_id: str,
        tbl_id: str,
        tagged_fields: Optional[List[TaggedFields]] = None,
    ):
        self.nspname = nspname
        self.relname = relname
        self.column_name = column_name
        self.type = type
        self.oid = oid
        self.attnum = attnum
        self.obj_id = obj_id
        self.tbl_id = tbl_id
        self.tagged_fields = tagged_fields

    def __str__(self):
        return (
            f"nspname={self.nspname}, "
            f"relname={self.relname}, "
            f"column_name={self.column_name}, "
            f"type={self.type}, oid={self.oid}, "
            f"attnum={self.attnum}, "
            f"obj_id={self.obj_id}, "
            f"tbl_id={self.tbl_id}"
        )

    @property
    def field_comment(self) -> Optional[str]:
        for field in self.tagged_fields:
            if (
                field.nspname == self.nspname
                and field.relname == self.relname
                and field.column_name == self.column_name
            ):
                if ":sens" in field.column_comment:
                    self.sensitive = True
                    return ":sens"
                elif ":nosens" in field.column_comment:
                    self.sensitive = False
                    return ":nosens"
        return

    def create_query(self, scan_mode: ScanMode, scan_partial_rows: int):
        query = (
            f'select distinct("{self.column_name}")::text from "{self.nspname}"."{self.relname}" '
            f'WHERE "{self.column_name}" is not null'
        )

        if scan_mode == ScanMode.PARTIAL:
            query = f"{query} LIMIT {str(scan_partial_rows)}"
        self.query = query

    def check_sens_pg_types(self):
        """Check if actual field type is sens."""
        for pg_type in SENS_PG_TYPES:
            if pg_type in self.type:
                return True
        return False

    async def get_row(self, pool):
        async with pool.acquire() as db_conn:
            if self.query:
                row_data = await db_conn.fetch(self.query)
                self.row_data = setof_to_list(row_data)


class SensFieldScan:
    def __init__(self, ctx):
        self.ctx = ctx

    async def get_tagged_fields(self, pool) -> List[TaggedFields]:
        """Get fields tagged sens and nosens."""
        query = """
        SELECT
            nspname AS schema_name,
            relname AS table_name,
            attname AS column_name,
            description AS column_comment
        FROM
            pg_description
            JOIN pg_attribute ON pg_description.objoid = pg_attribute.attrelid
                               AND pg_description.objsubid = pg_attribute.attnum
            JOIN pg_class ON pg_class.oid = pg_attribute.attrelid
            JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
        WHERE
            pg_class.relkind = 'r' AND pg_attribute.attnum > 0 AND NOT pg_attribute.attisdropped
            and (description like '%:sens%' OR description like '%:nosens%')
        ORDER BY
            nspname,
            relname,
            attname;
        """
        db_conn = await pool.acquire()
        try:
            query_res = await db_conn.fetch(query)
        finally:
            await db_conn.close()
        tagged_fields = [
            TaggedFields(
                nspname=record["schema_name"],
                relname=record["table_name"],
                column_name=record["column_name"],
                column_comment=record["column_comment"],
            )
            for record in query_res
        ]
        # await db_conn.close()
        # await pool.release(db_conn)
        return tagged_fields

    @staticmethod
    def check_skip_fields(dictionary_obj, fld):
        if "skip_rules" not in dictionary_obj:
            return True
        for v in dictionary_obj["skip_rules"]:
            schema_match = False
            tbl_match = False
            fld_match = False
            res = True
            if "schema" in v and fld["nspname"] == v["schema"]:
                schema_match = True
            if "table" in v and fld["relname"] == v["table"]:
                tbl_match = True
            if "fields" in v and fld["column_name"] in v["fields"]:
                fld_match = True
            if schema_match and tbl_match and fld_match:
                res = False

            if "fields" not in v and schema_match and tbl_match:
                res = False

            if "table" not in v and "fields" not in v and schema_match:
                res = False

            if not res:
                logger.debug(
                    "!!! ------> check_skip_fields: filtered fld %s by rule %s"
                    % (str(dict(fld)), str(v))
                )
                return res

        return True

    async def generate_scan_objs(self, dictionary_obj, pool):
        query = """
        -- generate task queue
        SELECT 
            n.nspname,
            c.relname,
            a.attname AS column_name,
            format_type(a.atttypid, a.atttypmod) as type,
            -- a.*
            c.oid, a.attnum,
            anon_funcs.digest(n.nspname || '.' || c.relname || '.' || a.attname, '', 'md5') as obj_id,
            anon_funcs.digest(n.nspname || '.' || c.relname, '', 'md5') as tbl_id
        FROM pg_class c
        JOIN pg_namespace n on c.relnamespace = n.oid
        JOIN pg_attribute a ON a.attrelid = c.oid
        JOIN pg_type t ON a.atttypid = t.oid
        LEFT JOIN pg_index i ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE
            a.attnum > 0
            AND c.relkind IN ('r', 'p')
            AND a.atttypid = t.oid
            AND n.nspname not in ('pg_catalog', 'information_schema', 'pg_toast')
            AND coalesce(i.indisprimary, false) = false
            AND row(c.oid, a.attnum) not in (
                SELECT
                    t.oid,
                    a.attnum --,
                    -- pn_t.nspname,
                    -- t.relname AS table_name,
                    -- a.attname AS column_name
                FROM pg_class AS t
                JOIN pg_attribute AS a ON a.attrelid = t.oid
                JOIN pg_depend AS d ON d.refobjid = t.oid AND d.refobjsubid = a.attnum
                JOIN pg_class AS s ON s.oid = d.objid
                JOIN pg_namespace AS pn_t ON pn_t.oid = t.relnamespace
                WHERE
                    t.relkind IN ('r', 'p')
                    AND s.relkind = 'S'
                    AND d.deptype = 'a'
                    AND d.classid = 'pg_catalog.pg_class'::regclass
                    AND d.refclassid = 'pg_catalog.pg_class'::regclass
            )
            -- AND c.relname = 'card_numbers'  -- debug
        ORDER BY 1, 2, a.attnum
        """
        db_conn = await pool.acquire()
        try:
            query_res = await db_conn.fetch(query)
        finally:
            await db_conn.close()
        tagged_fields = await self.get_tagged_fields(pool)

        sens_fields = [
            FieldInfo(**fld, tagged_fields=tagged_fields)
            for fld in query_res
            if self.check_skip_fields(dictionary_obj=dictionary_obj, fld=fld)
        ]
        sens_fields = [
            sens_field for sens_field in sens_fields if sens_field.check_sens_pg_types()
        ]

        return sens_fields

    async def prepare_dictionary_obj(self):
        self.ctx.dictionary_obj["data_const"]["constants"] = set(
            self.ctx.dictionary_obj["data_const"]["constants"]
        )

        regex_for_compile = []
        for v in self.ctx.dictionary_obj["data_regex"]["rules"]:
            regex_for_compile.append(re.compile(v))

        self.ctx.dictionary_obj["data_regex"]["rules"] = regex_for_compile.copy()

        regex_for_compile = []
        for v in self.ctx.dictionary_obj["field"]["rules"]:
            regex_for_compile.append(re.compile(v))

        self.ctx.dictionary_obj["field"]["rules"] = regex_for_compile.copy()

    async def check_sensitive_fld_names(self, fields_info: List[FieldInfo]):
        for field_info in fields_info:
            if "rules" in self.ctx.dictionary_obj["field"]:
                for rule in self.ctx.dictionary_obj["field"]["rules"]:
                    if re.search(rule, field_info.column_name) is not None:
                        logger.debug(
                            '!!! ------> check_sensitive_fld_names: match by "%s", removed %s'
                            % (str(rule), str(field_info))
                        )
                        # objs.remove(v)
                        self.ctx.create_dict_matches[field_info.obj_id] = field_info

            if "constants" in self.ctx.dictionary_obj["field"]:
                for rule in self.ctx.dictionary_obj["field"]["constants"]:
                    if rule == field_info.column_name:
                        logger.debug(
                            '!!! ------> check_sensitive_fld_names: match by "%s", removed %s'
                            % (str(rule), str(field_info))
                        )
                        fields_info.remove(field_info)
                        self.ctx.create_dict_matches[field_info.obj_id] = field_info

    def check_sensitive_data_in_fld(
        self, dictionary_obj, create_dict_matches, field_info: FieldInfo
    ) -> dict:
        fld_data = field_info.row_data
        if field_info.relname == "card_numbers":
            x = 1
        fld_data_set = set()
        dict_matches = {}
        for v in fld_data:
            if v is None:
                continue
            for word in v.split():
                if len(word) >= 5:
                    fld_data_set.add(word.lower())

        result = set.intersection(
            dictionary_obj["data_const"]["constants"], fld_data_set
        )
        if len(result) > 0:
            logger.debug(
                "========> Process: check_sensitive_data: match by constant %s , %s"
                % (str(result), str(field_info))
            )
            dict_matches[field_info.obj_id] = field_info

        for v in fld_data:
            if (
                field_info.obj_id not in dict_matches
                and field_info.obj_id not in create_dict_matches
            ):
                for r in dictionary_obj["data_regex"]["rules"]:
                    if v is not None and re.search(r, v) is not None:
                        logger.debug(
                            '========> Process: check_sensitive_data: match by "%s", %s, %s'
                            % (str(r), str(v), str(field_info))
                        )
                        dict_matches[field_info.obj_id] = field_info
            else:
                break

        return dict_matches

    #############################################################################################
    def add_metadict_rule(
        self, dictionary_obj: dict, field_info: FieldInfo, anon_rules: dict
    ):
        hash_func = "anon_funcs.digest(\"%s\", 'salt_word', 'md5')"  # by default use md5 with salt

        for fld_type, func in dictionary_obj["funcs"].items():
            if str(field_info.type).find(fld_type) > -1:
                hash_func = func

        res_hash_func = (
            hash_func
            if hash_func.find("%s") == -1
            else hash_func % field_info.column_name
        )

        if field_info.tbl_id not in anon_rules:
            anon_rules[field_info.tbl_id] = {
                "schema": field_info.nspname,
                "table": field_info.relname,
                "fields": {field_info.column_name: res_hash_func},
            }
        else:
            anon_rules[field_info.tbl_id]["fields"].update(
                {field_info.column_name: res_hash_func}
            )
        return anon_rules

    def create_output_dict(
        self, tasks, dictionary_obj, create_dict_matches, current_dir, output_dict_file
    ):
        result = PgAnonResult()
        result.result_code = ResultCode.DONE
        # create output dict
        output_dict = {}
        output_dict["dictionary"] = []
        anon_dict_rules = {}

        # ============================================================================================
        # Fill results based on processes
        # ============================================================================================
        for res in tasks:
            for field_info in res.values():
                anon_dict_rules = self.add_metadict_rule(
                    dictionary_obj, field_info, anon_dict_rules
                )

        # ============================================================================================
        # Fill results based on check_sensitive_fld_names
        # ============================================================================================
        for field_info in create_dict_matches.values():
            anon_dict_rules = self.add_metadict_rule(
                dictionary_obj, field_info, anon_dict_rules
            )
        # ============================================================================================

        for _, v in anon_dict_rules.items():
            output_dict["dictionary"].append(v)

        output_dict_file = open(
            os.path.join(current_dir, "dict", output_dict_file), "w"
        )
        output_dict_file.write(json.dumps(output_dict, indent=4))
        output_dict_file.close()

        return result

    async def get_row_data(self, fields_info: List[FieldInfo], pool):
        tasks = []
        # db_conn = await pool.acquire()

        for field_info in fields_info:
            tasks.append(asyncio.ensure_future(field_info.get_row(pool)))
        await asyncio.gather(*tasks)

    def check_sens_data(self, fields_info, processes):
        if processes > 1:
            with Pool(processes=processes) as pool:
                scan_results = pool.starmap(
                    self.check_sensitive_data_in_fld,
                    (
                        (
                            self.ctx.dictionary_obj,
                            self.ctx.create_dict_matches,
                            field_info,
                        )
                        for field_info in fields_info
                    ),
                )
            scan_results = [scan_result for scan_result in scan_results if scan_result]
            return scan_results

        scan_results = []
        for field_info in fields_info:

            scan_results.append(
                self.check_sensitive_data_in_fld(
                    self.ctx.dictionary_obj,
                    self.ctx.create_dict_matches,
                    field_info,
                )
            )
        scan_results = [scan_result for scan_result in scan_results if scan_result]
        return scan_results

    async def create_dict_impl(self):
        conn_params = self.ctx.conn_params
        processes = self.ctx.args.threads
        pool = await asyncpg.create_pool(
            **conn_params,
            min_size=self.ctx.args.threads,
            max_size=self.ctx.args.threads,
        )

        # TODO: Here we create obj
        fields_info: List[FieldInfo] = await self.generate_scan_objs(
            self.ctx.dictionary_obj, pool
        )
        await pool.close()
        if not fields_info:
            raise Exception("No objects for create dictionary!")

        await self.check_sensitive_fld_names(
            fields_info=fields_info
        )  # fill ctx.create_dict_matches

        for field_info in fields_info:
            field_info.create_query(
                scan_mode=self.ctx.args.scan_mode,
                scan_partial_rows=self.ctx.args.scan_partial_rows,
            )
        pool = await asyncpg.create_pool(
            **conn_params,
            min_size=self.ctx.args.threads,
            max_size=self.ctx.args.threads,
        )
        await self.get_row_data(fields_info=fields_info, pool=pool)
        await pool.close()

        fields_info = [field_info for field_info in fields_info if field_info.row_data]

        scan_results = self.check_sens_data(
            fields_info=fields_info, processes=processes
        )

        result = self.create_output_dict(
            tasks=scan_results,
            dictionary_obj=self.ctx.dictionary_obj,
            create_dict_matches=self.ctx.create_dict_matches,
            current_dir=self.ctx.current_dir,
            output_dict_file=self.ctx.args.output_dict_file,
        )

        return result

    async def create_dict(self):
        result = PgAnonResult()
        result.result_code = ResultCode.DONE
        logger.info("-------------> Started create_dict mode")

        try:
            dictionary_file = open(
                os.path.join(self.ctx.current_dir, "dict", self.ctx.args.dict_file), "r"
            )
            self.ctx.dictionary_content = dictionary_file.read()
            dictionary_file.close()
            self.ctx.dictionary_obj = eval(self.ctx.dictionary_content)
            await self.prepare_dictionary_obj()
        except:
            logger.error("<------------- create_dict failed\n" + exception_helper())
            result.result_code = ResultCode.FAIL
            return result

        try:
            result = await self.create_dict_impl()
        except:
            logger.error("<------------- create_dict failed\n" + exception_helper())
            result.result_code = ResultCode.FAIL
            return result

        if result.result_code == ResultCode.DONE:
            logger.info("<------------- Finished create_dict mode")
        return result
