-- 01_force_logging.sql — make the database FORCE LOGGING (mandatory for Data Guard)
WHENEVER SQLERROR EXIT FAILURE
SET ECHO ON FEEDBACK ON SERVEROUTPUT ON

DECLARE
  v_force VARCHAR2(3);
BEGIN
  SELECT force_logging INTO v_force FROM v$database;
  IF v_force <> 'YES' THEN
    EXECUTE IMMEDIATE 'ALTER DATABASE FORCE LOGGING';
    DBMS_OUTPUT.PUT_LINE('FORCE LOGGING enabled.');
  ELSE
    DBMS_OUTPUT.PUT_LINE('FORCE LOGGING already YES.');
  END IF;

  -- Supplemental logging — required for Logical Standby/GoldenGate; harmless for physical DG
  EXECUTE IMMEDIATE 'ALTER DATABASE ADD SUPPLEMENTAL LOG DATA';
EXCEPTION
  WHEN OTHERS THEN
    IF SQLCODE = -32588 THEN
      DBMS_OUTPUT.PUT_LINE('Supplemental log data already enabled.');
    ELSE
      RAISE;
    END IF;
END;
/

SELECT force_logging, supplemental_log_data_min FROM v$database;
EXIT SUCCESS;
