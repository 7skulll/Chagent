class Queue:
    def __init__(self, path):
        self.path = path
    def put(self, task):
        with open(self.path, "a") as file:
            file.write(task + "\n")
