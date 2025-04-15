import threading
import os

def run_file1():
    os.system('python3 token_gen.py')

def run_file2():
    os.system('python3 bot.py')

t1 = threading.Thread(target=run_file1)
t2 = threading.Thread(target=run_file2)

t1.start()
t2.start()

t1.join()
t2.join()