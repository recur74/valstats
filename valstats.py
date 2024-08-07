#! /usr/bin/env python
import copy
import json
import os
import time
from datetime import datetime, date
from functools import lru_cache
from statistics import median

import click
import jsons
import matplotlib.pyplot as plt
import numpy as np
import valo_api
from dateutil import parser, tz
from dotenv import load_dotenv

import elo
from auth import Auth, requests_retry_session
from database import file_to_object, get_session, Match, User

load_dotenv()

THROTTLE = 10  # Wait this many seconds between calls

RUNNING_AVERAGE = 50
AVERAGE_TIER = 12  # Gold 1
LAST_RANK_CHANGE = datetime.fromisoformat("2022-06-23T00:00:00+00:00").timestamp() * 1000

HENRIK_API = "https://api.henrikdev.xyz/valorant"
HENRIK_KEY = os.getenv('HENRIK_KEY')
auth = None

plt.rcParams['ytick.right'] = plt.rcParams['ytick.labelright'] = True
plt.rcParams['ytick.left'] = plt.rcParams['ytick.labelleft'] = False

global_elo_map = {
    3: 1040, 4: 1051, 5: 1093,
    6: 1099, 7: 1104, 8: 1109,
    9: 1140, 10: 1157, 11: 1166,
    12: 1174, 13: 1179, 14: 1184,
    15: 1190, 16: 1195, 17: 1201,
    18: 1206, 19: 1211, 20: 1216,
    21: 1221, 22: 1228, 23: 1243,
    24: 1250, 25: 1255, 26: 1260,
    27: 1280
}

valo_api.set_api_key(HENRIK_KEY)


def get_tier_elo(tier, elo_map):
    return elo_map.get(tier)


@lru_cache
def get_user_id(session):
    print("Getting user id", flush=True)
    user_id = session.query(User.id). \
        filter(User.name == auth.name). \
        filter(User.tag == auth.tag). \
        one_or_none()
    if user_id:
        return user_id[0]
    try:
        response = valo_api.get_account_details_by_name_v1(name=auth.name, tag=auth.tag)
    except valo_api.exceptions.valo_api_exception.ValoAPIException as e:
        if e.status == 404:
            print(f"Could not find user '{auth.name}#{auth.tag}'")
            return None
        raise e
    user_id = response.puuid
    session.add(User(id=user_id, name=auth.name, tag=auth.tag))
    session.commit()
    return user_id


@lru_cache
def get_user_mmr(user_id):
    try:
        response = valo_api.get_mmr_details_by_puuid_v2(region=auth.region, puuid=user_id)
        try:
            print(f"Fetched rank for user '{response.name}'", flush=True)
        except:
            print(f"Fetched rank for user '{user_id}'", flush=True)
        time.sleep(THROTTLE)
    except valo_api.exceptions.valo_api_exception.ValoAPIException as e:
        if e.status == 404:
            print(f"Could not find user '{user_id}'")
        else:
            print(f"Issue with finding mmr for '{user_id}': {str(e)}")
        return 0
    return response.current_data.currenttier


@lru_cache
def get_tier_by_number(number):
    tiers = get_competitive_tiers()
    return next(t for t in tiers if t.get('tier') == number)


@lru_cache
def get_competitive_tiers():
    season_url = "https://valorant-api.com/v1/seasons/competitive"
    response = requests_retry_session().get(season_url).json()
    season_tier_id = max(response.get('data', []), key=lambda x: x['startTime']).get('competitiveTiersUuid')
    url = f"https://valorant-api.com/v1/competitivetiers/{season_tier_id}"
    response = requests_retry_session().get(url).json()
    current_episode = response.get('data')
    tiers = current_episode.get('tiers')
    for t in tiers:
        t['tierName'] = t['tierName'].title()
    return current_episode.get('tiers')


@lru_cache
def get_maps():
    url = "https://valorant-api.com/v1/maps"
    response = requests_retry_session().get(url).json()
    return response.get('data', None)


@lru_cache
def get_map(map_url):
    maps = get_maps()
    map = next((m for m in maps if m['mapUrl'] == map_url or m['displayName'] == map_url), None)
    return map


@lru_cache
def get_agents():
    url = f"https://valorant-api.com/v1/agents"
    response = requests_retry_session().get(url).json()
    return response.get('data', None)


@lru_cache
def get_agent(uuid_or_name=None):
    agents = get_agents()
    agent = next((m for m in agents if m['uuid'] == uuid_or_name or m['displayName'] == uuid_or_name), None)
    return agent


@lru_cache
def get_all_weapons():
    url = "https://valorant-api.com/v1/weapons"
    response = requests_retry_session().get(url).json()
    return response.get('data', [])


@lru_cache
def get_weapon(name=None, uuid=None):
    weapons = get_all_weapons()
    key = 'displayName' if name else 'uuid'
    value = name.title() if name else uuid.lower()

    weapon = next((w for w in weapons if w[key] == value), None)
    if not weapon:
        return get_weapon(name='vandal')
    return weapon


def map_to_internal(matches):
    internal = {}
    for m in matches:
        internal[m['metadata']['matchid']] = m
        m['matchInfo'] = m['metadata']
        m['matchInfo']['matchId'] = m['metadata']['matchid']
        m['matchInfo']['queueID'] = m['metadata']['mode'].lower()
        m['matchInfo']['gameStartMillis'] = m['metadata']['game_start'] * 1000
        m['matchInfo']['mapId'] = m['metadata']['map']
        if m['matchInfo']['queueID'] == 'competitive':
            m['teams'] = [
                {'teamId': k, 'won': v['has_won'], 'roundsPlayed': v['rounds_won'] + v['rounds_lost'],
                 'roundsWon': v['rounds_won']} for k, v in m['teams'].items()
            ]
        m['players'] = [
            {k: v for k, v in p.items()} for p in m['players']['all_players']
        ]
        for p in m['players']:
            p['subject'] = p['puuid']
            p['competitiveTier'] = p['currenttier']
            p['characterId'] = p['character']
            p['teamId'] = p['team'].lower()
        for k in m['kills']:
            k['killer'] = k['killer_puuid']
            k['victim'] = k['victim_puuid']
            k['finishingDamage'] = {}
            k['finishingDamage']['damageItem'] = k['damage_weapon_id']
    return internal


def get_game_history(session, exclude=[]):
    print("Fetching matches", flush=True)
    user_id = get_user_id(session)
    result = []
    for typ in ('deathmatch', 'competitive', 'teamdeathmatch'):
        response = valo_api.get_match_history_by_puuid_v3(region=auth.region, puuid=user_id, size=10, game_mode=typ)
        for match in response:
            if match.metadata.matchid not in exclude:
                if typ in ('deathmatch', 'teamdeathmatch'):
                    insert_competitive_tiers(match)
                result.append(match)
    print(f"Found {len(result)} new games", flush=True)
    return map_to_internal(jsons.dump(result))


def insert_competitive_tiers(deathmatch):
    for p in deathmatch.players.all_players:
        tier = get_user_mmr(p.puuid)
        p.currenttier = tier


def process_comp_matches(matches, user_id):
    print("Processing competitive matches", flush=True)
    games = []
    last_rank = AVERAGE_TIER
    for match in matches.values():
        if match['matchInfo']['queueID'] != 'competitive':
            continue
        ranks = []
        winning_team = next((t for t in match['teams'] if t['won'] is True), None)
        scores = match['teams'][0]['roundsWon'], match['teams'][0]['roundsPlayed'] - match['teams'][0]['roundsWon']
        starttime = datetime.utcfromtimestamp(match.get('matchInfo').get('gameStartMillis') / 1000).replace(
            tzinfo=tz.tzutc()).isoformat()
        map = get_map(match.get('matchInfo').get('mapId')).get('displayName')
        game = {'date': starttime,
                'map': map}
        for player in match.get('players', []):
            if player.get('subject') == user_id:
                game['agent'] = get_agent(player.get('characterId')).get('displayName')
                game['rank'] = get_tier_by_number(player.get('competitiveTier')).get('tierName')
                game['rank_raw'] = player.get('competitiveTier')
                if not winning_team:
                    game['result'] = 'Draw {w}-{l}'.format(w=max(scores), l=min(scores))
                elif player['teamId'] == winning_team['teamId']:
                    game['result'] = 'Win {w}-{l}'.format(w=max(scores), l=min(scores))
                else:
                    game['result'] = 'Loss {l}-{w}'.format(w=max(scores), l=min(scores))
            if player.get('competitiveTier', 0) != 0 and player.get('subject') != user_id:
                ranks.append(player.get('competitiveTier'))
        avg = sum(ranks) / len(ranks) if len(ranks) != 0 else last_rank
        last_rank = avg
        game['mmr'] = get_tier_by_number(int(avg)).get('tierName')
        game['mmr_raw'] = avg
        game['progress'] = int((avg - int(avg)) * 100)
        games.append(game)
    return games


def get_main_weapon(match, user_id):
    # player_stats = next(ps for ps in match['roundResults'][0]['playerStats'] if ps['subject'] == user_id)
    # print(player_stats)
    user_kills = [k for k in match['kills'] if k['killer'] == user_id]
    weapons = {}
    for k in user_kills:
        weapon = k.get('finishingDamage', {}).get('damageItem')
        if not weapons.get(weapon):
            weapons[weapon] = 0
        weapons[weapon] += 1
    if not weapons:
        return "Unknown"
    main_weapon = max(weapons, key=weapons.get)
    return get_weapon(uuid=main_weapon).get('displayName')
    # return weaponmap.get(main_weapon, main_weapon)


def get_dm_weight(main_weapon, avg_tier, date_of_match):
    tier_damp = AVERAGE_TIER
    weapon_damp = 6000
    baseline_weapon = 'Vandal'
    days_ago = (date.today() - parser.parse(date_of_match).date()).days
    tier_decay = 1 - (days_ago / 3650)

    tier_weight = (avg_tier * tier_decay + tier_damp) / (AVERAGE_TIER + tier_damp)
    # print(f"tier_weight for {avg_tier:.2f}: {tier_weight:.2f}")
    baseline_weapon_cost = get_weapon(name=baseline_weapon).get('shopData').get('cost')
    # print(main_weapon)
    main_weapon_cost = get_weapon(name=main_weapon).get('shopData').get('cost')
    weapon_weight = (weapon_damp + baseline_weapon_cost) / (weapon_damp + main_weapon_cost)
    # print(f"weapon_weight for {main_weapon}: {weapon_weight:.2f}")
    # print(f"total weight: {tier_weight * weapon_weight:.2f}")
    # print("")
    return tier_weight * weapon_weight


def elo_gain_for_match_for_user(match, user_id, elo_map=global_elo_map,
                                initial_elo=get_tier_elo(AVERAGE_TIER, global_elo_map), excluded_users=[]):
    match_elo_score = {'Unknown': {'expected': 0, 'actual': 0}}
    main_weapon = get_main_weapon(match, user_id)
    for kill in match['kills']:
        if 'victim' not in kill:
            kill['victim'] = kill['victim_puuid']
        if kill['killer'] != user_id and kill['victim'] != user_id:
            continue
        if kill['killer'] in excluded_users or kill['victim'] in excluded_users:
            continue
        opponent_uuid = next(
            iter(u for u in [kill.get('victim'), kill.get('killer')] if u != user_id and u is not None), None)
        if opponent_uuid is None:
            continue
        opponent_main_weapon = get_main_weapon(match, opponent_uuid)
        kill_weapon = get_weapon(uuid=kill.get('finishingDamage', {}).get('damageItem')).get('displayName')
        if kill_weapon != main_weapon or opponent_main_weapon != main_weapon:
            continue
        if not match_elo_score.get(kill_weapon):
            match_elo_score[kill_weapon] = {'expected': 0, 'actual': 0}
        if kill['killer'] == user_id:
            victim = next(iter(p for p in match['players'] if p['subject'] == kill.get('victim')))
            opponent_tier = victim.get('competitiveTier')
            if is_unranked(opponent_tier):
                continue
            match_elo_score[kill_weapon]['expected'] += elo.expected(initial_elo, get_tier_elo(opponent_tier, elo_map))
            match_elo_score[kill_weapon]['actual'] += 1
        if kill.get('victim') == user_id:
            opponent_tier = next(
                iter(p.get('competitiveTier') for p in match['players'] if p['subject'] == kill['killer']))
            if is_unranked(opponent_tier):
                continue
            match_elo_score[kill_weapon]['expected'] += elo.expected(initial_elo, get_tier_elo(opponent_tier, elo_map))
    if main_weapon not in match_elo_score or len(match_elo_score[main_weapon]) == 0:
        return 0
    new_elo = elo.elo(initial_elo, match_elo_score[main_weapon]['expected'],
                      match_elo_score[main_weapon]['actual'], k=1)
    return new_elo - initial_elo


def is_unranked(tier):
    return tier is None or tier == 0


def process_dms_for_elo(matches, user_id):
    print("Processing deathmatch games for elo", flush=True)
    elos = {'Unknown': [0]}
    for match in matches.values():
        if match['matchInfo']['queueID'] not in ('deathmatch', 'team deathmatch'):
            continue
        main_weapon = get_main_weapon(match, user_id)
        if main_weapon not in elos:
            elos[main_weapon] = [get_tier_elo(AVERAGE_TIER, global_elo_map)]
        elo_gain = elo_gain_for_match_for_user(match=match,
                                               user_id=user_id,
                                               elo_map=global_elo_map,
                                               initial_elo=elos.get(main_weapon)[-1])
        elos[main_weapon].append(elos.get(main_weapon)[-1] + elo_gain)
    return elos


def _calibration_score(matches, elo_map, tier=AVERAGE_TIER, weapon='Vandal', excluded_users=[]):
    res = 0
    for match in matches:
        match_res = 0
        players = [p for p in match.get('players') if
                   p.get('competitiveTier') == tier and
                   get_main_weapon(match, p.get('subject')) == weapon and p.get('subject') not in excluded_users]
        if len(players) < 2:
            continue
        for player in players:
            match_res += elo_gain_for_match_for_user(match, player.get('subject'), elo_map, get_tier_elo(tier, elo_map),
                                                     excluded_users=excluded_users)
        res += match_res / len(players)
    return res


def _score_all_tiers(matches, elo_map, excluded_users=[]):
    result = {'total': 0, 'tiers': {}}
    for tier in elo_map.keys():
        result['tiers'][tier] = _calibration_score(matches, elo_map, tier, excluded_users=excluded_users)
    result['total'] = sum([abs(v) for v in result['tiers'].values()])
    return result


def _adjust_elo(tier, amount, min_diff, elo_map=global_elo_map):
    new_elo_map = copy.copy(elo_map)
    MIN_TIER = min([k for k in global_elo_map.keys()])
    MAX_TIER = max([k for k in global_elo_map.keys()])
    new_elo_map[tier] += amount
    if amount > 0 and tier < MAX_TIER and new_elo_map[tier] + min_diff >= new_elo_map[tier + 1]:
        new_elo_map = _adjust_elo(tier + 1, 1, min_diff, elo_map=new_elo_map)
    if amount < 0 and tier > MIN_TIER and elo_map[tier] - min_diff <= elo_map[tier - 1]:
        new_elo_map = _adjust_elo(tier - 1, -1, min_diff, elo_map=new_elo_map)
    return new_elo_map


def calibrate_elo(matches, init_elo_map, excluded_users=[]):
    NUDGE_DISTANCE = 1
    MIN_TIER_DIFF = 5
    matches = [m for m in matches.values() if m['matchInfo']['queueID'] in ('deathmatch', 'team deathmatch') and
               m['matchInfo']['gameStartMillis'] > LAST_RANK_CHANGE]
    print(f"Calibrating on {len(matches)} DM games", flush=True)

    best_elo_map = copy.copy(init_elo_map)
    scores = _score_all_tiers(matches, init_elo_map, excluded_users=excluded_users)

    best_score = scores['total']
    print(f"Initial score {best_score}")
    iteration = 1
    while True:
        print(f"Iteration {iteration}")
        print(f"Current best elo-map {best_elo_map}")
        test_scores = {}
        for tier in init_elo_map.keys():
            print(f"Checking tier {get_tier_by_number(tier).get('tierName')}({tier})", flush=True)
            score = scores['tiers'][tier]
            if score == 0:
                continue
            amount = int(NUDGE_DISTANCE * (score / abs(score)))
            test_elo_map = _adjust_elo(tier=tier, amount=amount, min_diff=MIN_TIER_DIFF, elo_map=best_elo_map)
            test_scores[tier] = (
                _score_all_tiers(matches, test_elo_map, excluded_users=excluded_users)['total'], amount)
            print(test_scores[tier])
        smallest = sorted(test_scores, key=lambda y: abs(test_scores[y][0]))[0]
        print(
            f"The best change was {get_tier_by_number(smallest).get('tierName')}[{test_scores[smallest][1]}] with {test_scores[smallest][0]}",
            flush=True)
        if test_scores[smallest][0] >= best_score:
            print("No improvement. Stopping.")
            break
        best_score = test_scores[smallest][0]
        best_elo_map = _adjust_elo(tier=smallest, amount=test_scores[smallest][1], min_diff=MIN_TIER_DIFF,
                                   elo_map=best_elo_map)
        iteration += 1


def process_dm_matches(auth, matches, user_id):
    print("Processing deathmatch games", flush=True)
    games = []
    for match in matches.values():
        if match['matchInfo']['queueID'] not in ('deathmatch', 'team deathmatch'):
            continue
        main_weapon = get_main_weapon(match, user_id)
        starttime = datetime.utcfromtimestamp(match.get('matchInfo').get('gameStartMillis') / 1000).replace(
            tzinfo=tz.tzutc()).isoformat()
        map = get_map(match.get('matchInfo').get('mapId')).get('displayName')
        game = {'mode': match['matchInfo']['queueID'],
                'date': starttime,
                'map': map,
                'weapon': main_weapon}
        tiers = [p.get('competitiveTier') or AVERAGE_TIER for p in match.get('players') if p.get('subject') != user_id]
        avg_tier = round(sum(tiers) / len(tiers) if len(tiers) else AVERAGE_TIER, 2)
        me = next(p for p in match.get('players') if p.get('subject') == user_id)
        game['agent'] = get_agent(me.get('characterId')).get('displayName')
        game['kills'] = me['stats']['kills']
        game['deaths'] = me['stats']['deaths']
        game['score'] = me['stats']['score']
        game['assists'] = me['stats']['assists']
        game['avg_tier'] = avg_tier
        # print(f"Average Tier: {get_tier_by_number(round(avg_tier)).get('tierName')}")
        # print(main_weapon)
        if game['kills'] > 0:
            if game['deaths'] > 0:
                game['performance'] = round(
                    ((game['kills']) * get_dm_weight(main_weapon, avg_tier, starttime)) / (
                            game['deaths'] + game['assists']), 2)
                game['kd'] = round(game['kills'] / game['deaths'], 2)
            else:
                game['performance'] = 10
                game['kd'] = 10
            games.append(game)
    return games


def print_dm_games(games: list):
    games = sorted(games, key=lambda i: i['date'])
    running_average = []
    for game in games:
        running_average.append(game['performance'])
        gamedate = parser.parse(game['date']).astimezone().replace(tzinfo=None)
        print(game['mode'].upper())
        print(gamedate.isoformat(sep=' ', timespec='minutes'))
        print(game['agent'] + '@' + game['map'])
        print(game['weapon'])
        print("{}/{}/{} - {}".format(game['kills'], game['deaths'], game['assists'], game['kd']))
        print(f"{get_tier_by_number(round(game['avg_tier'])).get('tierName')} - {game['performance']}")
        if len(running_average) > RUNNING_AVERAGE:
            running_average = running_average[-RUNNING_AVERAGE:]
            # print("Running average: {}".format(round(sum(running_average) / len(running_average), 2)))
            print("Running median: {}".format(round(median(running_average), 2)))
        print("-----", flush=True)


def print_comp_games(games: list):
    games = sorted(games, key=lambda i: i['date'])
    for game in games:
        gamedate = parser.parse(game['date']).astimezone().replace(tzinfo=None)
        print("RANKED")
        print(gamedate.isoformat(sep=' ', timespec='minutes'))
        print(game['agent'] + '@' + game['map'])
        print("Result: " + game['result'])
        print("Rank: " + game['rank'])
        print("MMR: " + game['mmr'] + "+" + str(game['progress']))
        print("-----", flush=True)


def plot_comp_games(username: str, games: list):
    games = sorted(games, key=lambda i: i['date'])
    if not games:
        return
    mmr = [g['mmr_raw'] for g in games]
    ranks = [g['rank_raw'] for g in games]
    dates = [g['date'] for g in games]
    en_dates = [i for i, d in enumerate(dates)]

    plt.figure()
    plt.plot(dates, mmr, label="Est. MMR")
    plt.plot(dates, ranks, label="Rank")
    z = np.polyfit(en_dates, mmr, 1)
    p = np.poly1d(z)
    plt.plot(en_dates, p(en_dates), "r--", label="Rank Trend")
    plt.yticks(list(t.get('tier') for t in get_competitive_tiers()),
               list(t.get('tierName') for t in get_competitive_tiers()))
    plt.xticks(dates, en_dates)
    plt.gca().xaxis.set_major_locator(plt.MaxNLocator(10))
    plt.grid(visible=True, which='major', axis='y', color='#EEEEEE', linestyle='-')

    plt.xlabel('Matches')
    plt.ylabel('Rank')

    plt.legend()
    plt.title(f'Competitive RR vs MMR for {username}')


def plot_dm_games(username, games, weapon=None, metric='kd'):
    games_w = {}
    if weapon:
        for g in games:
            _weapon = g.get('weapon')
            if not games_w.get(_weapon):
                games_w[_weapon] = []
            games_w[_weapon].append(g)
        plot_dm_games_for_weapon(username, games_w[weapon], weapon, metric)
    else:
        plot_dm_games_for_weapon(username, games, "all weapons", metric)


def plot_elo_dm_games(username, games, weapon):
    elos = games.get(weapon)
    if not elos:
        print(f"No DM games found with {weapon}")
        return
    en = list(range(len(elos)))
    plt.figure()
    plt.plot(en, elos, 'b')
    tiers = [t for t in get_competitive_tiers() if "Un" not in t.get('tierName')]
    tier_elo_values = [get_tier_elo(t.get('tier'), global_elo_map) for t in tiers]
    tier_names = [t.get('tierName') for t in tiers]
    plt.yticks(tier_elo_values, tier_names)
    plt.grid(visible=True, which='major', axis='y', color='#EEEEEE', linestyle='-')
    plt.xlabel('Matches')
    plt.ylabel('Elo')
    plt.title(f'Deathmatch Elo for {username} with {weapon}')


def plot_dm_games_for_weapon(username, games, weapon, metric='kd'):
    games = sorted(games, key=lambda i: i['date'])
    if not games:
        return
    metric_values = [g[metric] for g in games]
    ra = []

    running_avg = []
    for game in games:
        running_avg.append(game[metric])
        if len(running_avg) > RUNNING_AVERAGE:
            running_avg = running_avg[-RUNNING_AVERAGE:]
            ra.append(round(median(running_avg), 2))
            # ra.append(round(sum(running_avg) / len(running_avg), 2))
        else:
            ra.append(None)

    dates = [g['date'] for g in games]
    en_dates = [i for i, d in enumerate(dates)]
    plt.figure()
    plt.scatter(dates, [g['kd'] for g in games], color='gray', label=f"kd")
    plt.scatter(dates, metric_values, color='blue', label=f"{metric}")
    plt.plot(dates, ra, color='orange', label="Running Average")
    if len(en_dates) > 1:
        z = np.polyfit(en_dates, metric_values, 1)
        p = np.poly1d(z)
        plt.plot(en_dates, p(en_dates), "r--", label=f"{metric} Trend")

    # plt.yticks(list(rankmap.keys()), list(rankmap.values()))
    plt.ylim(bottom=0)
    plt.xticks(dates, en_dates)
    plt.gca().xaxis.set_major_locator(plt.MaxNLocator(10))
    plt.grid(visible=True, which='major', axis='y', color='#EEEEEE', linestyle='-')

    plt.xlabel('Matches')
    plt.ylabel(metric)
    plt.legend()
    plt.title(f'Deathmatch {metric} for {username} with {weapon}')


@click.command()
@click.argument('username')  # In-game user
@click.option('--zone', default='eu', help="Valorant zone (eu, na etc)")
@click.option('--plot/--no-plot', default=True, help='Plot the result')
@click.option('--print/--no-print', 'print_', default=False, help='Print the games to terminal')
@click.option('--db-name', default=None, help="Database name and path. Default is ./{username}.db")
@click.option('--weapon', default=None, help="Show dm stats for this weapon only",
              type=click.Choice([d.get('displayName').lower() for d in get_all_weapons()]))
@click.option('--calibrate/--no-calibrate', default=False)
def valstats(username, zone, plot, print_, db_name, weapon, calibrate):
    if not db_name:
        db_name = f"{username}.sqlitedb"
    weapon = weapon.title() if weapon else weapon
    name, tag = username.split('#')
    global auth
    auth = Auth(name, tag, zone)
    session = get_session(f"{db_name}")
    user_id = get_user_id(session)
    if not user_id:
        return
    print("Loading database")
    if session.query(Match.id).count() == 0:
        matches = file_to_object(db_name) or {}
        for key, data in matches.items():
            session.add(Match(id=key, data=json.dumps(data)))
        session.commit()
    else:
        results = session.query(Match).all()
        matches = {res.id: json.loads(res.data) for res in results}

    new_matches = get_game_history(session, exclude=list(matches.keys()))
    if new_matches:
        for key, data in new_matches.items():
            session.add(Match(id=key, data=json.dumps(data)))
        session.commit()
        matches.update(new_matches)
    matches = sorted(matches.values(), key=lambda m: m.get('matchInfo').get('gameStartMillis'))
    matches = {m.get('matchInfo').get('matchid'): m for m in matches}

    if calibrate:
        calibrate_elo(matches, global_elo_map, excluded_users=[user_id])
        return

    elo_dm_matches = process_dms_for_elo(matches, user_id)
    comp_matches = process_comp_matches(matches, user_id)
    dm_matches = process_dm_matches(auth, matches, user_id)
    if print_:
        print_comp_games(comp_matches)
        print_dm_games(dm_matches)
    if plot:
        plot_elo_dm_games(username, elo_dm_matches, weapon)
        # plot_dm_games(username, dm_matches, weapon, 'kd')
        plot_dm_games(username, dm_matches, weapon, 'performance')
        plot_comp_games(username, comp_matches)
        plt.show()


if __name__ == '__main__':
    valstats()
