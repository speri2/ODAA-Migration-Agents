-- 02_archivelog_check.sql — verify ARCHIVELOG mode (dbaascli should already have set this)
WHENEVER SQLERROR EXIT FAILURE
SET ECHO ON FEEDBACK ON SERVEROUTPUT ON

DECLARE
  v_log_mode VARCHAR2(20);
BEGIN
  SELECT log_mode INTO v_log_mode FROM v$database;
  IF v_log_mode <> 'ARCHIVELOG' THEN
    DBMS_OUTPUT.PUT_LINE('ERROR: database is in ' || v_log_mode || ', expected ARCHIVELOG.');
    -- Force a non-zero exit
    RAISE_APPLICATION_ERROR(-20001, 'Database is not in ARCHIVELOG mode');
  ELSE
    DBMS_OUTPUT.PUT_LINE('ARCHIVELOG mode confirmed.');
  END IF;
END;
/

SELECT log_mode, open_mode FROM v$database;
SELECT name, value FROM v$parameter
 WHERE name IN ('db_recovery_file_dest','db_recovery_file_dest_size','log_archive_dest_1');
EXIT SUCCESS;
