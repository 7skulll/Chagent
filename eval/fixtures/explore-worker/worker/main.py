from worker.queue import Queue
DEFAULT_PORT = 9093
def build_worker():
    return Queue("jobs.db")
def main():
    worker = build_worker()
    print(DEFAULT_PORT, worker.path)
