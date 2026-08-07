import contextlib, io, os, json, time as _time, socket as _socket, logging
from datetime import datetime, timedelta

# 端口锁：防止重复启动，bind失败时agentmain会直接崩溃退出
# reload时mod.__dict__保留_lock，跳过重复绑定
if os.environ.get('GENERICAGENT_SKIP_SCHEDULER_LOCK') != '1':
    try: _lock
    except NameError:
        _lock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _lock.bind(('127.0.0.1', 45762)); _lock.listen(1)

# dedup: prevent same-task re-trigger within one check cycle
_triggered_tasks: dict = {}
_TRIGGERED_COOLDOWN = 60  # seconds
_ts = __import__('time').time

INTERVAL = 120
ONCE = False

_dir = os.path.dirname(os.path.abspath(__file__))
TASKS = os.path.join(_dir, '../sche_tasks')
DONE  = os.path.join(_dir, '../sche_tasks/done')
_LOG  = os.path.join(_dir, '../sche_tasks/scheduler.log')

os.makedirs(DONE, exist_ok=True)
_logger = logging.getLogger('scheduler')
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    _fh = logging.FileHandler(_LOG, encoding='utf-8')
    _fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s',
                                        datefmt='%Y-%m-%d %H:%M'))
    _logger.addHandler(_fh)

# 默认最大延迟窗口（小时），超过此时间不触发
DEFAULT_MAX_DELAY = 6
_L4_INTERVAL = 600      # L4 archive check interval (seconds)
_l4_t = 0               # last L4 archive time
_MINING_INTERVAL = 600  # salient mining interval (seconds, 10min)
_mining_t = 0           # last mining run time
_DS_INTERVAL = 1200  # DeepSeek watch interval (seconds, 20min)


def _l4_result_is_actionable(result):
    result = result or {}
    try:
        return any(int(result.get(key, 0) or 0) > 0 for key in (
            'processed', 'errors', 'deleted_raw',
        ))
    except (TypeError, ValueError):
        return False


def _parse_cooldown(repeat):
    """解析repeat为冷却时间(比实际周期略短,防漂移)"""
    if repeat == 'once': return timedelta(days=999999)
    if repeat in ('daily', 'weekday'): return timedelta(hours=20)
    if repeat == 'weekly': return timedelta(days=6)
    if repeat == 'monthly': return timedelta(days=27)
    if repeat.startswith('every_'):
        try:
            parts = repeat.split('_')
            n = int(parts[1].rstrip('hdm'))
            u = parts[1][-1]
            if u == 'h': return timedelta(hours=n)
            if u == 'm': return timedelta(minutes=n)
            if u == 'd': return timedelta(days=n)
        except (ValueError, IndexError):
            pass  # fall through to warning below
    _logger.warning(f'Unknown repeat type: {repeat}, fallback to 20h cooldown')
    return timedelta(hours=20)

def _last_run(tid, done_files):
    """找最近一次执行时间"""
    latest = None
    for df in done_files:
        if not df.endswith(f'_{tid}.md'): continue
        try:
            t = datetime.strptime(df[:15], '%Y-%m-%d_%H%M')
            if latest is None or t > latest: latest = t
        except: continue
    return latest

def check():
    # L4 archive cron (check every 60s, batch_process filters recent files)
    global _l4_t
    if _time.time() - _l4_t > _L4_INTERVAL:
        _l4_t = _time.time()
        try:
            import sys; sys.path.insert(0, os.path.join(_dir, '../memory/L4_raw_sessions'))
            from compress_session import batch_process
            raw_dir = os.path.join(_dir, '../temp/model_responses')
            with contextlib.redirect_stdout(io.StringIO()):
                r = batch_process(raw_dir, dry_run=False)
            if _l4_result_is_actionable(r):
                print('[L4 cron] '
                      f'processed={r.get("processed", 0)} '
                      f'errors={r.get("errors", 0)} '
                      f'deleted={r.get("deleted_raw", 0)}')
        except Exception as e:
            # 失败时回退时间戳，让下一轮立即重试而非等一个完整间隔
            _l4_t -= _L4_INTERVAL
            _logger.error(f'L4 archive failed: {e}', exc_info=True)

    # L4 → L2 salient mining cron (every 10min, incremental)
    global _mining_t
    if _time.time() - _mining_t > _MINING_INTERVAL:
        _mining_t = _time.time()
        try:
            import sys; sys.path.insert(0, os.path.join(_dir, '../memory/L4_raw_sessions'))
            from salient_mining import run
            r = run(dry_run=False)
            if r.get('new_sessions', 0):
                print(f'[MINE cron] +{r["new_sessions"]} sessions, '
                      f'+{r.get("l2_updates", 0)} L2 updates, '
                      f'+{r.get("new_emotional_events", 0)} emotions')
        except Exception as e:
            _logger.error(f'Salient mining failed: {e}')

    if not os.path.isdir(TASKS): return None

    now = datetime.now()
    os.makedirs(DONE, exist_ok=True)
    done_files = set(os.listdir(DONE))
    for f in sorted(os.listdir(TASKS)):
        if not f.endswith('.json'): continue
        tid = f[:-5]
        try:
            with open(os.path.join(TASKS, f), encoding='utf-8') as fp:
                task = json.loads(fp.read())
        except Exception as e:
            _logger.error(f'JSON parse error for {f}: {e}')
            continue
        if not task.get('enabled', False): continue
        
        repeat = task.get('repeat', 'daily')
        sched = task.get('schedule', '00:00')
        try:
            h, m = map(int, sched.split(':'))
        except Exception as e:
            _logger.error(f'Invalid schedule format in {f}: {sched!r} ({e})')
            continue
        
        # weekday任务：周末跳过
        if repeat == 'weekday' and now.weekday() >= 5: continue
        
        # 还没到schedule时间就跳过
        last = _last_run(tid, done_files)
        cooldown = _parse_cooldown(repeat)
        overdue = last and (now - last) >= cooldown * 1.5
        if now.hour < h or (now.hour == h and now.minute < m):
            if overdue:
                _logger.info(f"CATCHUP {tid}: last={last}, overdue, forcing pre-schedule run")
            else:
                continue
        if not overdue:
            max_delay = task.get("max_delay_hours", DEFAULT_MAX_DELAY)
            sched_minutes = h * 60 + m
            now_minutes = now.hour * 60 + now.minute
            if (now_minutes - sched_minutes) > max_delay * 60:
                _logger.info(f"SKIP {tid}: {now_minutes - sched_minutes}min past schedule, exceeds max_delay={max_delay}h")
                continue
        if last and (now - last) < cooldown: continue
        
        # dedup: skip if triggered within _TRIGGERED_COOLDOWN
        if tid in _triggered_tasks and (_ts() - _triggered_tasks[tid]) < _TRIGGERED_COOLDOWN:
            _logger.debug(f'DEDUP {tid}: skipped')
            continue
        _triggered_tasks[tid] = _ts()
        expired = [k for k in _triggered_tasks if _ts() - _triggered_tasks[k] > _TRIGGERED_COOLDOWN * 2]
        for k in expired: del _triggered_tasks[k]

        # 触发
        _logger.info(f'TRIGGER {tid} (repeat={repeat}, schedule={sched}, '
                     f'last_run={last})')
        ts = now.strftime('%Y-%m-%d_%H%M')
        rpt = os.path.join(DONE, f'{ts}_{tid}.md')
        prompt = task.get('prompt', '')
        return (f'[定时任务] {tid}\n'
                f'[报告路径] {rpt}\n\n'
                f'先读 scheduled_task_sop 了解执行流程，然后执行以下任务：\n\n'
                f'{prompt}\n\n'
                f'完成后将执行报告写入 {rpt}。')

    return None

# ---- DeepSeek watch daemon thread (guaranteed ~20min cadence, silent unless changed) ----
def _ds_watch_loop():
    import sys
    _time.sleep(60)  # let agent boot; first check ~1min after start
    while True:
        try:
            if _dir not in sys.path:
                sys.path.insert(0, _dir)
            from deepseek_watch import run_all
            note = run_all()
            if note:
                _logger.info('DeepSeek Watch CHANGE:\n' + note)
        except Exception as e:
            _logger.error('DeepSeek watch failed: %s', e)
        _time.sleep(_DS_INTERVAL)


def _start_ds_watch():
    import threading
    for t in threading.enumerate():
        if t.name == 'ds_watch_thread' and t.is_alive():
            return  # already running (survives scheduler reload)
    th = threading.Thread(target=_ds_watch_loop, name='ds_watch_thread', daemon=True)
    th.start()
    _logger.info('DeepSeek watch thread started (20min interval)')


_start_ds_watch()
