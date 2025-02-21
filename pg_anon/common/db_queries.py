import re

from pg_anon.common.dto import FieldInfo


def get_query_limit(limit: int) -> str:
    return f"LIMIT {limit}" if limit is not None and limit > 0 else ""


def get_query_get_scan_fields(limit: int = None, count_only: bool = False):
    if not count_only:
        fields = """
            SELECT DISTINCT
            n.nspname,
            c.relname,
            a.attname AS column_name,
            format_type(a.atttypid, a.atttypmod) as type,
            c.oid, a.attnum,
            anon_funcs.digest(n.nspname || '.' || c.relname || '.' || a.attname, '', 'md5') as obj_id,
            anon_funcs.digest(n.nspname || '.' || c.relname, '', 'md5') as tbl_id
        """
        order_by = 'ORDER BY 1, 2, a.attnum' if count_only else ''
    else:
        fields = "SELECT COUNT(*)"
        order_by = ''

    query_limit = get_query_limit(limit)

    return f"""
    {fields}
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
                a.attnum
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
    {order_by}
    {query_limit}
    """


def get_data_from_field(field_info: FieldInfo, limit: int = None, condition: str = None, not_null: bool = True) -> str:
    """
    Build query for receiving data from table
    :param field_info: Field info
    :param limit: batch size
    :param condition: specific WHERE condition for receiving data
    :param not_null: filter for receiving only not null values
    :return: Returns raw SQL query
    """

    conditions = []
    query_condition = ''
    need_where = True

    if condition:
        condition_without_special_characters = re.sub('[^A-Z0-9]+', '', condition.upper())
        if condition_without_special_characters.startswith('WHERE'):
            need_where = False
        conditions.append(condition)

    if not_null:
        conditions.append(f'\"{field_info.column_name}\" is NOT NULL')

    if conditions:
        query_condition = 'WHERE ' if need_where else ''
        query_condition += ' and '.join(conditions)

    query_limit = get_query_limit(limit)

    query = f"""
    SELECT distinct(substring(\"{field_info.column_name}\"::text, 1, 8196))
    FROM \"{field_info.nspname}\".\"{field_info.relname}\"
    {query_condition}
    {query_limit}
    """

    return query
