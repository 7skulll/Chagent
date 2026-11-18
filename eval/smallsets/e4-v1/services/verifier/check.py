def verify(artifacts):
    answer = artifacts / 'answer.txt'
    return answer.is_file() and answer.read_bytes().strip() == b'42'
