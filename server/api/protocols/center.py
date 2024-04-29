# params: SYSTEM_URL, TEAM_TOKEN, TIMEOUT_QUERY
import requests as rq
from models import FlagSubmit, FlagStatus, Flag


def submit_flags(flags: list[Flag], config):
    url = 'http://' + config.get('SYSTEM_URL') + '/flags'

    print(flags)

    for flag in flags:
        print(flag)
        f = flag.flag
        res = rq.put(url, headers={
            'X-Team-Token': config.get('TEAM_TOKEN')}, data=["{f}"])

        yield FlagSubmit(flag['flag'], FlagStatus.ACCEPTED, res)
