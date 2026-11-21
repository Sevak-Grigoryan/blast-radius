import time, json, os
_cpu = {}
_t = [0.0]
def pytest_runtest_logstart(nodeid, location):
    _t[0] = time.process_time()
def pytest_runtest_logfinish(nodeid, location):
    _cpu[nodeid] = round(time.process_time() - _t[0], 4)
def pytest_sessionfinish(session):
    json.dump(_cpu, open(os.environ["CPU_OUT"], "w"))
