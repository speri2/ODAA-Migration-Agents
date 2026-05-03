-- 03_standby_redo_logs.sql — provision standby redo logs on the primary.
--
-- Rule of thumb: SRL groups = (online redo log groups per thread + 1) per thread.
-- For RAC with 2 threads and 2 ORLs/thread we need at least 6 SRLs.
-- Caller passes ${SRL_COUNT} (per-thread) and ${SRL_SIZE_MB}.
WHENEVER SQLERROR EXIT FAILURE
SET ECHO ON FEEDBACK ON SERVEROUTPUT ON LINESIZE 200

DECLARE
  v_threads     NUMBER;
  v_existing    NUMBER;
  v_orl_size_mb NUMBER;
  v_size_mb     NUMBER := ${SRL_SIZE_MB};
  v_per_thread  NUMBER := ${SRL_COUNT};
  v_thread      NUMBER;
  v_groups_to_add NUMBER;
  v_next_grp    NUMBER;
BEGIN
  SELECT MAX(thread#) INTO v_threads FROM v$thread;
  SELECT COUNT(*) INTO v_existing FROM v$standby_log;
  SELECT MAX(bytes/1024/1024) INTO v_orl_size_mb FROM v$log;

  -- Always size SRLs >= largest online redo log
  IF v_orl_size_mb IS NOT NULL AND v_orl_size_mb > v_size_mb THEN
    v_size_mb := v_orl_size_mb;
  END IF;

  DBMS_OUTPUT.PUT_LINE('Threads='||v_threads||' existing_SRL='||v_existing||
                       ' size_mb='||v_size_mb||' target_per_thread='||v_per_thread);

  FOR v_thread IN 1..v_threads LOOP
    DECLARE
      v_have NUMBER;
    BEGIN
      SELECT COUNT(*) INTO v_have FROM v$standby_log WHERE thread# = v_thread;
      v_groups_to_add := GREATEST(v_per_thread - v_have, 0);

      WHILE v_groups_to_add > 0 LOOP
        SELECT NVL(MAX(group#),0) + 1
          INTO v_next_grp
          FROM (
            SELECT group# FROM v$log
            UNION ALL
            SELECT group# FROM v$standby_log
          );
        EXECUTE IMMEDIATE
          'ALTER DATABASE ADD STANDBY LOGFILE THREAD ' || v_thread ||
          ' GROUP ' || v_next_grp ||
          ' SIZE ' || v_size_mb || 'M';
        DBMS_OUTPUT.PUT_LINE('Added SRL group ' || v_next_grp || ' to thread ' || v_thread);
        v_groups_to_add := v_groups_to_add - 1;
      END LOOP;
    END;
  END LOOP;
END;
/

COLUMN member FORMAT A80
SELECT thread#, group#, bytes/1024/1024 AS mb, status FROM v$standby_log ORDER BY thread#, group#;
EXIT SUCCESS;
