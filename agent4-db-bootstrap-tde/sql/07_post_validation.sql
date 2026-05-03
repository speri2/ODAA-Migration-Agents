-- 07_post_validation.sql — emits AGENT4_KV>>KEY=value<<AGENT4_KV markers parsed by post_validate.py.
-- All values must come out as a single line per key so the regex can pick them up cleanly.
--
-- Bindings:
--   ${PDB_NAME}  — target PDB
WHENEVER SQLERROR EXIT FAILURE
SET ECHO OFF FEEDBACK OFF VERIFY OFF HEADING OFF PAGESIZE 0 LINESIZE 32767 TRIMSPOOL ON SERVEROUTPUT ON
SET TERMOUT ON

-- ============================================================
-- CDB-level facts
-- ============================================================
SELECT 'AGENT4_KV>>DB_NAME=' || name              || '<<AGENT4_KV' FROM v$database;
SELECT 'AGENT4_KV>>DB_UNIQUE_NAME=' || db_unique_name || '<<AGENT4_KV' FROM v$database;
SELECT 'AGENT4_KV>>DB_VERSION=' || version         || '<<AGENT4_KV' FROM v$instance WHERE ROWNUM = 1;
SELECT 'AGENT4_KV>>OPEN_MODE=' || open_mode        || '<<AGENT4_KV' FROM v$database;
SELECT 'AGENT4_KV>>ARCHIVELOG=' || log_mode        || '<<AGENT4_KV' FROM v$database;
SELECT 'AGENT4_KV>>FORCE_LOGGING=' || force_logging || '<<AGENT4_KV' FROM v$database;
SELECT 'AGENT4_KV>>FLASHBACK_ON=' || flashback_on  || '<<AGENT4_KV' FROM v$database;

-- ============================================================
-- Wallet / TDE
-- ============================================================
SELECT 'AGENT4_KV>>WALLET_STATUS=' || NVL(MAX(status), 'UNKNOWN')  || '<<AGENT4_KV'
  FROM v$encryption_wallet WHERE con_id IN (0,1);
SELECT 'AGENT4_KV>>WALLET_TYPE='   || NVL(MAX(wallet_type), 'UNKNOWN') || '<<AGENT4_KV'
  FROM v$encryption_wallet WHERE con_id IN (0,1);
SELECT 'AGENT4_KV>>WALLET_LOCATION=' || NVL(MAX(wrl_parameter), '') || '<<AGENT4_KV'
  FROM v$encryption_wallet WHERE con_id IN (0,1);

-- Master key id in the target PDB (empty if none)
COLUMN x FORMAT A200
SELECT 'AGENT4_KV>>MASTER_KEY_ID=' || NVL(MAX(key_id), '') || '<<AGENT4_KV'
  FROM v$encryption_keys
 WHERE con_id = (SELECT con_id FROM v$pdbs WHERE UPPER(name) = UPPER('${PDB_NAME}'))
   AND activation_time IS NOT NULL;

-- ============================================================
-- Standby Redo Logs
-- ============================================================
SELECT 'AGENT4_KV>>SRL_COUNT=' || COUNT(*) || '<<AGENT4_KV' FROM v$standby_log;
SELECT 'AGENT4_KV>>SRL_GROUP_SIZES=' ||
       LISTAGG(group# || ':' || ROUND(bytes/1024/1024) || 'M', ',')
         WITHIN GROUP (ORDER BY group#) || '<<AGENT4_KV'
  FROM v$standby_log;

-- ============================================================
-- Password file / RAC instances
-- ============================================================
SELECT 'AGENT4_KV>>PWFILE_LOCATION=' || NVL(MAX(value), '') || '<<AGENT4_KV'
  FROM v$parameter WHERE name = 'remote_login_passwordfile';

SELECT 'AGENT4_KV>>INSTANCE_COUNT=' || COUNT(DISTINCT instance_number) || '<<AGENT4_KV'
  FROM gv$instance;

-- Encrypted tablespace count (sanity — should be > 0 once TDE is active and any TS is encrypted)
SELECT 'AGENT4_KV>>ENC_TBS_COUNT=' || COUNT(*) || '<<AGENT4_KV'
  FROM v$encrypted_tablespaces;

-- ============================================================
-- PDB
-- ============================================================
SELECT 'AGENT4_KV>>PDB_OPEN_MODE='  || open_mode  || '<<AGENT4_KV'
  FROM v$pdbs WHERE UPPER(name) = UPPER('${PDB_NAME}');
SELECT 'AGENT4_KV>>PDB_RESTRICTED=' || NVL(restricted,'NO') || '<<AGENT4_KV'
  FROM v$pdbs WHERE UPPER(name) = UPPER('${PDB_NAME}');

EXIT SUCCESS;
