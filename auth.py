from utils import requests_retry_session
import urllib


class Auth:
    def __init__(self, name, tag, region):
        self.headers = None
        self.session = requests_retry_session(status_forcelist=(500, 502, 504, 403))
        self.name = urllib.parse.quote_plus(name)
        self.tag = urllib.parse.quote_plus(tag)
        self.region = region
