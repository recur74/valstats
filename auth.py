from utils import requests_retry_session


class Auth:
    def __init__(self, name, tag, region):
        self.headers = None
        self.session = requests_retry_session(status_forcelist=(500, 502, 504, 403))
        self.name = name
        self.tag = tag
        self.region = region
