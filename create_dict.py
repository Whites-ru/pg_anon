from common import *


async def create_dict():
    pass


"""
SELECT 
	n.nspname,
	c.relname,
	a.attname AS column_name,
	format_type(a.atttypid, a.atttypmod) as type,
	-- a.*
	c.oid, a.attnum
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
ORDER BY 1, 2, a.attnum
"""

"""
SELECT
	t.oid,
    pn_t.nspname,
    t.relname AS table_name,
    a.attname AS column_name
    -- s.relname AS sequence_name
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
"""