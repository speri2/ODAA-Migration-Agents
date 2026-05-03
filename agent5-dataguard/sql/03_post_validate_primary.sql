-- 03_post_validate_primary.sql — emit AGENT5_KV>>KEY=value<<AGENT5_KV markers from the primary.
-- Bindings:
--   ${STANDBY_UNIQUE_NAME}
WHENEVER SQLERROR EXIT FAILURE
SET ECHO OFF FEEDBACK OFF VERIFY OFF HEADING OFF PAGESIZE 0 LINESIZE 32767 TRIMSPOOL ON

SELECT 'AGENT5_KV>>PRI_ROLE=' || database_role || '<<AGENT5_KV' FROM v$database;
SELECT 'AGENT5_KV>>PRI_OPEN_MODE=' || open_mode || '<<AGENT5_KV' FROM v$database;
SELECT 'AGENT5_KV>>PRI_PROTECTION_MODE=' || protection_mode || '<<AGENT5_KV' FROM v$database;
SELECT 'AGENT5_KV>>PRI_PROTECTION_LEVEL=' || protection_level || '<<AGENT5_KV' FROM v$database;

SELECT 'AGENT5_KV>>PRI_DEST2_STATUS=' || NVL(MAX(status), 'NA') || '<<AGENT5_KV'
  FROM v$archive_dest WHERE dest_id = 2;

SELECT 'AGENT5_KV>>PRI_LAST_SEQUENCE=' || NVL(MAX(sequence#), 0) || '<<AGENT5_KV'
  FROM v$archived_log
 WHERE dest_id = 1 AND archived = 'YES';

SELECT 'AGENT5_KV>>PRI_GAP_COUNT=' || COUNT(*) || '<<AGENT5_KV'
  FROM v$archive_gap;

EXIT SUCCESS;
