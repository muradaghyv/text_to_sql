-- Migration 003: privilege-to-table mapping and per-employee table access

CREATE TABLE IF NOT EXISTS privilege_table_access (
    privilege_id  int8          NOT NULL,
    db_id         int8          NOT NULL REFERENCES registered_databases(id),
    table_name    varchar(255)  NOT NULL,
    match_score   float4        NULL,
    PRIMARY KEY (privilege_id, db_id, table_name)
);

CREATE TABLE IF NOT EXISTS emp_table_access (
    emp_id      int8          NOT NULL,
    db_id       int8          NOT NULL REFERENCES registered_databases(id),
    table_name  varchar(255)  NOT NULL,
    PRIMARY KEY (emp_id, db_id, table_name)
);
