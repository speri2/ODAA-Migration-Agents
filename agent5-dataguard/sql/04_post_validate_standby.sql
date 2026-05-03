-- 04_post_validate_standby.sql — standby-side validation markers
WHENEVER SQLERROR EXIT FAILURE
SET ECHO OFF FEEDBACK OFF VERIFY OFF HEADING OFF PAGESIZE 0 LINESIZE 32767 TRIMSPOOL ON

SELECT 'AGENT5_KV>>STBY_ROLE=' || database_role || '<<AGENT5_KV' FROM v$database;
SELECT 'AGENT5_KV>>STBY_OPEN_MODE=' || open_mode || '<<AGENT5_KV' FROM v$database;

-- MRP / managed recovery process status (waiting for log, applying, etc.)
SELECT 'AGENT5_KV>>STBY_MRP_STATUS=' || NVL(MAX(status), 'NOT_STARTED') || '<<AGENT5_KV'
  FROM v$managed_standby
 WHERE process LIKE 'MRP%';

-- Last applied / received sequence
SELECT 'AGENT5_KV>>STBY_LAST_APPLIED_SEQ=' || NVL(MAX(sequence#), 0) || '<<AGENT5_KV'
  FROM v$archived_log
 WHERE applied = 'YES';

SELECT 'AGENT5_KV>>STBY_LAST_RECEIVED_SEQ=' || NVL(MAX(sequence#), 0) || '<<AGENT5_KV'
  FROM v$archived_log;

-- Apply lag in minutes (best-effort; real lag comes from dgmgrl)
SELECT 'AGENT5_KV>>STBY_DELAY_MINS=' ||
       NVL(ROUND((SYSDATE - MAX(completion_time)) * 24 * 60, 1), 0) ||
       '<<AGENT5_KV'
  FROM v$archived_log WHERE applied = 'YES';

-- TDE wallet on standby — must be OPEN for redo apply on encrypted tablespaces
SELECT 'AGENT5_KV>>STBY_WALLET_STATUS=' || NVL(MAX(status), 'UNKNOWN') || '<<AGENT5_KV'
  FROM v$encryption_wallet WHERE con_id IN (0, 1);

EXIT SUCCESS;
