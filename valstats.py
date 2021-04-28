import requests
import matplotlib.pyplot as plt
import click
import dateutil.parser
from functools import lru_cache
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

@lru_cache()
def get_userid_from_name(username: str) -> str:
    """
    :param username: Valorant username in format "NAME#TAG"
    :return: userid used for blitz.gg api
    """
    USERURL = "https://valorant.iesdev.com/player/{username}"
    username = username.replace('#', '-').lower()
    response = requests_retry_session().get(USERURL.format(username=username)).json()
    userid = response.get('subject')
    return userid


def fetch_match_data(username: str, acts: list) -> list:
    """
    Fetches match data for a user and list of acts
    :param username: Valorant username in format "NAME#TAG"
    :param acts: List if acts in format ["2.1", "2.2"]
    :return
    """
    MATCHURL = "https://valorant.iesdev.com/matches/{userid}?" \
               "offset={offset}&queues=competitive&type=subject&actId={actid}"
    matches = []
    userid = get_userid_from_name(username)
    for act in sorted(acts):
        offset = 0
        actid = actmap[act]
        response = requests_retry_session().get(MATCHURL.format(offset=offset, userid=userid, actid=actid)).json()
        while len(response.get('data')) == 20:
            matches.extend(response.get('data', []))
            offset += 20
            response = requests_retry_session().get(MATCHURL.format(offset=offset, userid=userid, actid=actid)).json()
        matches.extend(response.get('data', []))
    return matches


def process_matches(username, matches: list) -> list:
    games = []
    userid=get_userid_from_name(username)
    for match in matches:
        ranks = []
        winning_team = next((t for t in match['teams'] if t['won'] is True), None)
        game = {'date': match.get('startedAt'), # dateutil.parser.parse(match.get('startedAt')),
                'map': mapmap.get(match.get('map'), match.get('map')).title()}
        for player in match.get('players', []):
            if player.get('subject') == userid:
                game['agent'] = agentmap.get(player.get('characterId'), player.get('characterId'))
                game['rank'] = rankmap[player.get('competitiveTier')]
                game['rank_raw'] = player.get('competitiveTier')
                if not winning_team:
                    game['result'] = 'Draw'
                elif player['teamId'] == winning_team['teamId']:
                    game['result'] = 'Win'
                else:
                    game['result'] = 'Loss'
            if player.get('competitiveTier') and player.get('subject') != userid:
                ranks.append(player.get('competitiveTier'))
        avg = sum(ranks) / len(ranks)
        game['mmr'] = rankmap[int(avg)]
        game['mmr_raw'] = avg
        game['progress'] = int((avg - int(avg)) * 100)
        games.append(game)
    return games


def print_games(games: list):
    games = sorted(games, key=lambda i: i['date'])
    for game in games:
        gamedate = dateutil.parser.parse(game['date']).astimezone().replace(tzinfo=None)
        print(gamedate.isoformat(sep=' ', timespec='minutes'))
        print(game['agent'] + '@' + game['map'])
        print("Result: " + game['result'])
        print("Rank: " + game['rank'])
        print("MMR: " + game['mmr'] + "+" + str(game['progress']))
        print("-----")


def plot_games(username: str, games: list):
    games = sorted(games, key=lambda i: i['date'])
    mmr = [g['mmr_raw'] for g in games]
    ranks = [g['rank_raw'] for g in games]
    dates = [g['date'] for g in games]

    plt.plot(dates, mmr, label="Est. MMR")
    plt.plot(dates, ranks, label="Rank")
    plt.yticks(list(rankmap.keys()), list(rankmap.values()))
    plt.xticks(dates, [i for i, d in enumerate(dates)])
    plt.gca().xaxis.set_major_locator(plt.MaxNLocator(10))
    plt.grid(b=True, which='major', axis='y', color='#EEEEEE', linestyle='-')

    plt.xlabel('Matches')
    plt.ylabel('Rank')

    plt.legend()
    plt.title('RR vs MMR for {username}'.format(username=username))

    plt.show()


def requests_retry_session(retries=5, backoff_factor=1, status_forcelist=(500, 502, 504), session=None,):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


actmap = {
    '1.1': "3f61c772-4560-cd3f-5d3f-a7ab5abda6b3",
    '1.2': "0530b9c4-4980-f2ee-df5d-09864cd00542",
    '1.3': "46ea6166-4573-1128-9cea-60a15640059b",
    '2.1': "97b6e739-44cc-ffa7-49ad-398ba502ceb0",
    '2.2': "ab57ef51-4e59-da91-cc8d-51a5a2b9b8ff",
    '2.3': "52e9749a-429b-7060-99fe-4595426a0cf7",
}

rankmap = {
    0: 'Unranked',
    3: 'Iron 1',
    4: 'Iron 2',
    5: 'Iron 3',
    6: 'Bronze 1',
    7: 'Bronze 2',
    8: 'Bronze 3',
    9: 'Silver 1',
    10: 'Silver 2',
    11: 'Silver 3',
    12: 'Gold 1',
    13: 'Gold 2',
    14: 'Gold 3',
    15: 'Platinum 1',
    16: 'Platinum 2',
    17: 'Platinum 3',
    18: 'Diamond 1',
    19: 'Diamond 2',
    20: 'Diamond 3',
}

mapmap = {
    'port': 'icebox'
}

agentmap = {
    '1e58de9c-4950-5125-93e9-a0aee9f98746': 'Killjoy',
    '9f0d8ba9-4140-b941-57d3-a7ad57c6b417': 'Brimstone',
    '7f94d92c-4234-0a36-9646-3a87eb8b5c89': 'Yoru',
    '5f8d3a7f-467b-97f3-062c-13acf203c006': 'Breach',
    'eb93336a-449b-9c1b-0a54-a891f7921d69': 'Phoenix',
    '707eab51-4836-f488-046a-cda6bf494859': 'Viper',
    'f94c3b30-42be-e959-889c-5aa313dba261': 'Raze',
    '6f2a04ca-43e0-be17-7f36-b3908627744d': 'Skye',
    '117ed9e3-49f3-6512-3ccf-0cada7e3823b': 'Cypher',
    'ded3520f-4264-bfed-162d-b080e2abccf9': 'Sova',
    '320b2a48-4d9b-a075-30f1-1f93a9b638fa': 'Sova',
    '41fb69c1-4189-7b37-f117-bcaf1e96f1bf': 'Astra',
    '569fdd95-4d10-43ab-ca70-79becc718b46': 'Sage',
    'a3bfb853-43b2-7238-a4f1-ad90e9e46bcc': 'Reyna',
    '8e253930-4c05-31dd-1b6c-968525494517': 'Omen',
    'add6443a-41bd-e414-f6ad-e58d267f4e95': 'Jett',
}


@click.command()
@click.argument('username')
@click.option('--plot/--no-plot', default=True, help='Plot the result')
@click.option('--print/--no-print', default=True, help='Print the games to terminal')
@click.option('--act', default=None, help='Specify act in format "2.1" (Episode 2, Act 1)', type=str)
def valstats(username, plot, print, act):
    acts = [act] if act else actmap.keys()
    matches = fetch_match_data(username, acts)
    matches = process_matches(username, matches)
    if print:
        print_games(matches)
    if plot:
        plot_games(username, matches)


if __name__ == '__main__':
    valstats()
