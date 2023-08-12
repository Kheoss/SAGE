import csv
import datetime
import glob
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict, OrderedDict
from itertools import accumulate

import matplotlib.pyplot as plt
import numpy as np
import requests

# Import attack stages, mappings and alert signatures
sys.path.insert(0, './signatures')
from signatures.attack_stages import MicroAttackStage, MacroAttackStage
from signatures.mappings import micro, micro_inv, macro, macro_inv, micro2macro, mcols, small_mapping, rev_smallmapping, verbose_micro, ser_groups
from signatures.alert_signatures import usual_mapping, unknown_mapping, ccdc_combined, attack_stage_mapping


IANA_CSV_FILE = "https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.csv"
IANA_NUM_RETRIES = 5
DB_PATH = "./ports.json"
SAVE = True
DOCKER = True


def _get_attack_stage_mapping(signature):
    result = MicroAttackStage.NON_MALICIOUS
    if signature in usual_mapping.keys():
        result = usual_mapping[signature]
    elif signature in unknown_mapping.keys():
        result = unknown_mapping[signature]
    elif signature in ccdc_combined.keys():
        result = ccdc_combined[signature]
    else:
        for k, v in attack_stage_mapping.items():
            if signature in v:
                result = k
                break
    return micro_inv[str(result)]

 
def _most_frequent(serv):
    max_frequency = 0
    most_frequent_service = None
    for s in serv:
        frequency = serv.count(s)
        if frequency > max_frequency:
            most_frequent_service = s
            max_frequency = frequency
    return most_frequent_service


# Step 0: Download the IANA port-service mapping"""
def load_iana_mapping():
    # Perform the first request and in case of a failure retry the specified number of times
    for attempt in range(IANA_NUM_RETRIES + 1):
        response = requests.get(IANA_CSV_FILE)
        if response.ok:
            content = response.content.decode("utf-8")
            break
        elif attempt < IANA_NUM_RETRIES:
            print('Could not download IANA ports. Retrying...')
        else:
            raise RuntimeError('Cannot download IANA ports')
    table = csv.reader(content.splitlines())

    # Drop headers (service name, port, protocol, description, ...)
    next(table)

    # Note that ports might have holes
    ports = {}
    for row in table:
        # Drop missing port number, Unassigned and Reserved ports
        if row[1] and 'Unassigned' not in row[3]:  # and 'Reserved' not in row[3]:
            
            # Split range in single ports
            if '-' in row[1]:
                low_port, high_port = map(int, row[1].split('-'))
            else:
                low_port = high_port = int(row[1])

            for port in range(low_port, high_port + 1):
                ports[port] = {
                    "name": row[0] if row[0] else "unknown",
                    "description": row[3] if row[3] else "---",
                }
    return ports


def _readfile(fname):
    with open(fname, 'r') as f:
        unparsed_data = json.load(f)
        
    unparsed_data = unparsed_data[::-1]
    return unparsed_data


# Step 1.1: Parse the input alerts
def _parse(unparsed_data, alert_labels=[], slim=False):
    FILTER = False
    badIP = '169.254.169.254'
    __cats = set()
    __ips = set()
    __hosts = set()
    parsed_data = []

    prev = -1
    for d in unparsed_data:
        if 'result' in d and '_raw' in d['result']:
            raw = json.loads(d['result']['_raw'])
        elif '_raw' in d:
            raw = json.loads(d['_raw'])
        else:
            raw = d

        if raw['event_type'] != 'alert':
            continue

        if 'host' in raw:
            host = raw['host']
        elif 'host' in d:
            host = d['host'][3:]
        else:
            host = 'dummy'

        dt = datetime.datetime.strptime(raw['timestamp'], '%Y-%m-%dT%H:%M:%S.%f%z')  # 2018-11-03T23:16:09.148520+0000
        diff_dt = 0.0 if prev == -1 else round((dt - prev).total_seconds(), 2)
        prev = dt

        sig = raw['alert']['signature']
        cat = raw['alert']['category']

        # Filter out the alert that occurs way too often
        if cat == 'Attempted Information Leak' and FILTER:
            continue

        src_ip = raw['src_ip']
        src_port = None if 'src_port' not in raw.keys() else raw['src_port']
        dst_ip = raw['dest_ip']
        dst_port = None if 'dest_port' not in raw.keys() else raw['dest_port']

        # Filter out mistaken alerts / uninteresting alerts
        if src_ip == badIP or dst_ip == badIP or cat == 'Not Suspicious Traffic':
            continue

        if not slim:
            mcat = _get_attack_stage_mapping(sig)
            parsed_data.append((diff_dt, src_ip, src_port, dst_ip, dst_port, sig, cat, host, dt, mcat))
        else:
            parsed_data.append((diff_dt, src_ip, src_port, dst_ip, dst_port, sig, cat, host, dt))

        __cats.add(cat)
        __ips.add(src_ip)
        __ips.add(dst_ip)
        __hosts.add(host)

    '''_cats = [(id,c) for (id,c) in enumerate(__cats)]
    for (i,c) in _cats:
        if c not in cats.keys():
            cats[c] = 0 if len(cats.values())==0 else max(cats.values())+1
    _ips = [(id,ip) for (id,ip) in enumerate(__ips)]
    for (i,ip) in _ips:
        if ip not in ips.keys():
            ips[ip] = 0 if len(ips.values())==0 else max(ips.values())+1
    _hosts = [(id,h) for (id,h) in enumerate(__hosts)]
    for (i,h) in _hosts:
        if h not in hosts.keys():
            hosts[h] = 0 if len(hosts.values())==0 else max(hosts.values())+1'''

    print('Reading # alerts: ', len(parsed_data))

    if slim:
        print(len(parsed_data), len(alert_labels))
        j = 0
        for i, al in enumerate(alert_labels):
            spl = al.split(',')
            source = spl[0]
            dest = spl[1]
            mcat = int(spl[-1][:-1])
            cat = spl[2]

            if source == badIP or dest == badIP or cat == 'Not Suspicious Traffic':
                continue
            if spl[2] == 'Attempted Information Leak' and FILTER:
                continue

            if source == parsed_data[j][1] and dest == parsed_data[j][3]:
                parsed_data[j] += (mcat,)
            j += 1
    parsed_data = sorted(parsed_data, key=lambda x: x[8])  # Sort alerts into ascending order
    return parsed_data


def _plot_alert_filtering(unfiltered_alerts, filtered_alerts):
    original, remaining = dict(), dict()
    original_mcat = [x[9] for x in unfiltered_alerts]
    for i in original_mcat:
        original[i] = original.get(i, 0) + 1

    remaining_mcat = [x[9] for x in filtered_alerts]
    for i in remaining_mcat:
        remaining[i] = remaining.get(i, 0) + 1
    if MicroAttackStage.NON_MALICIOUS.value in original:
        remaining[MicroAttackStage.NON_MALICIOUS.value] = 0  # mcat that has been filtered (non-malicious)

    # Use ordered dictionaries to make sure that the labels (categories) are aligned
    b1 = OrderedDict(sorted(original.items()))
    b2 = OrderedDict(sorted(remaining.items()))

    plt.figure(figsize=(20, 20))
    plt.gcf().subplots_adjust(bottom=0.2)  # To fit the x-labels

    # Set width and height of bar
    bar_width = 0.4
    bars1 = [x for x in b1.values()]
    bars2 = [x for x in b2.values()]

    # Set position of bar on x-axis
    r1 = np.arange(len(bars1))
    r2 = [x + bar_width for x in r1]

    # Make the plot
    plt.bar(r1, bars1, color='skyblue', width=bar_width, edgecolor='white', label='Raw')
    plt.bar(r2, bars2, color='salmon', width=bar_width, edgecolor='white', label='Cleaned')

    labels = [micro[x].split('.')[1] for x in b1.keys()]

    # Add xticks in the middle of the group bars
    plt.ylabel('Frequency', fontweight='bold', fontsize='20')
    plt.xlabel('Alert categories', fontweight='bold', fontsize='20')
    plt.xticks([(x + bar_width / 2) for x in r1], labels, fontsize='10', rotation='vertical')
    plt.yticks(fontsize='20')
    plt.title('High-frequency Alert Filtering', fontweight='bold', fontsize='20')

    # Create legend & show graphic
    plt.legend(prop={'size': 20})
    plt.show()
    return


# Step 1.2: Remove duplicate alerts (defined by the alert_filtering_window parameter)
def _remove_duplicates(unfiltered_alerts, plot=False, gap=1.0):
    filtered_alerts = [unfiltered_alerts[x] for x in range(1, len(unfiltered_alerts))
                       if unfiltered_alerts[x][9] != MicroAttackStage.NON_MALICIOUS.value  # Filter out non-malicious alerts
                       and not (unfiltered_alerts[x][0] <= gap  # Diff from previous alert is less than gap sec
                                and unfiltered_alerts[x][1] == unfiltered_alerts[x - 1][1]  # Same srcIP
                                and unfiltered_alerts[x][3] == unfiltered_alerts[x - 1][3]  # Same destIP
                                and unfiltered_alerts[x][5] == unfiltered_alerts[x - 1][5]  # Same suricata category
                                and unfiltered_alerts[x][2] == unfiltered_alerts[x - 1][2]  # Same srcPort
                                and unfiltered_alerts[x][4] == unfiltered_alerts[x - 1][4])]  # Same destPort
    if plot:
        _plot_alert_filtering(unfiltered_alerts, filtered_alerts)

    print('Filtered # alerts (remaining):', len(filtered_alerts))
    return filtered_alerts


# Step 1: Read the input alerts
def load_data(path_to_alerts, filtering_window):
    _team_alerts = []
    _team_labels = []
    files = glob.glob(path_to_alerts + "/*.json")
    print('About to read json files...')
    if len(files) < 1:
        print('No alert files found.')
        sys.exit()
    for f in files:
        name = os.path.basename(f)[:-5]
        print(name)
        _team_labels.append(name)

        parsed_alerts = _parse(_readfile(f), [])
        parsed_alerts = _remove_duplicates(parsed_alerts, gap=filtering_window)

        # EXP: Limit alerts by timing is better than limiting volume because each team is on a different scale.
        # 50% alerts for one team end at a diff time than for others
        end_time_limit = 3600 * end_hour       # Which hour to end at?
        start_time_limit = 3600 * start_hour   # Which hour to start from?

        first_ts = parsed_alerts[0][8]
        start_times.append(first_ts)

        filtered_alerts = [x for x in parsed_alerts if (((x[8] - first_ts).total_seconds() <= end_time_limit)
                                                        and ((x[8] - first_ts).total_seconds() >= start_time_limit))]
        _team_alerts.append(filtered_alerts)

    return _team_alerts, _team_labels


# Plotting for each team, how many categories are consumed
def plot_histogram(_team_alerts, _team_labels):
    # Choice of: Suricata category usage or Micro attack stage usage?
    SURICATA_SUMMARY = False
    suricata_categories = {'A Network Trojan was detected': 0, 'Generic Protocol Command Decode': 1, 'Attempted Denial of Service': 2,
                           'Attempted User Privilege Gain': 3, 'Misc activity': 4, 'Attempted Administrator Privilege Gain': 5,
                           'access to a potentially vulnerable web application': 6, 'Information Leak': 7, 'Web Application Attack': 8,
                           'Successful Administrator Privilege Gain': 9, 'Potential Corporate Privacy Violation': 10,
                           'Detection of a Network Scan': 11, 'Not Suspicious Traffic': 12, 'Potentially Bad Traffic': 13,
                           'Attempted Information Leak': 14}

    micro_attack_stages_codes = [x for x, _ in micro.items()]
    micro_attack_stages = [y for _, y in micro.items()]

    if SURICATA_SUMMARY:
        num_categories = len(suricata_categories)
        percentages = [[0] * len(suricata_categories) for _ in range(len(_team_alerts))]
    else:
        num_categories = len(micro_attack_stages)
        percentages = [[0] * len(micro_attack_stages) for _ in range(len(_team_alerts))]
    indices = np.arange(num_categories)    # The x locations for the groups
    bar_width = 0.75       # The width of the bars: can also be len(x) sequence

    for tid, team in enumerate(_team_alerts):
        for alert in team:
            # if alert[9] == 999:
            #    continue
            if SURICATA_SUMMARY:
                # if suricata_cats[alert[6]] != 14:
                percentages[tid][suricata_categories[alert[6]]] += 1
            else:
                percentages[tid][micro_attack_stages_codes.index(alert[9])] += 1
        for i, acat in enumerate(percentages[tid]):
            percentages[tid][i] = acat / len(team)
    plots = []
    for tid, team in enumerate(_team_alerts):
        if tid == 0:
            plot = plt.bar(indices, percentages[tid], bar_width)
        elif tid == 1:
            plot = plt.bar(indices, percentages[tid], bar_width, bottom=percentages[tid - 1])
        else:
            index = [x for x in range(tid)]
            bottom = np.add(percentages[0], percentages[1])
            for i in index[2:]:
                bottom = np.add(bottom, percentages[i]).tolist()
            plot = plt.bar(indices, percentages[tid], bar_width, bottom=bottom)
        plots.append(plot)

        # TODO: Decide whether to put it like this or normalize over columns
    plt.ylabel('Percentage of occurrence')
    plt.title('Frequency of alert category')
    if SURICATA_SUMMARY:
        plt.xticks(indices, ('c0', 'c1', 'c2', 'c3', 'c4', 'c5', 'c6', 'c7', 'c8', 'c9', 'c10', 'c11', 'c12', 'c13', 'c14'))
    else:
        plt.xticks(indices, [x.split('.')[1] for x in micro_attack_stages], rotation='vertical')
    plt.tick_params(axis='x', which='major', labelsize=8)
    plt.tick_params(axis='x', which='minor', labelsize=8)
    # plt.yticks(np.arange(0, 13000, 1000))
    plt.legend([plot[0] for plot in plots], _team_labels)
    plt.tight_layout()
    plt.savefig('data_histogram-' + experiment_name + '.png')
    # plt.show()


def _group_alerts_per_team(_team_alerts):
    """Reorganise alerts for each attacker per team"""
    team_data = dict()
    for tid, team in enumerate(team_alerts):
        host_alerts = dict()  # (attacker, victim) -> alerts

        for alert in team:
            # Alert format: (diff_dt, src_ip, src_port, dst_ip, dst_port, sig, cat, host, ts, mcat)
            src_ip, dst_ip, signature, ts, mcat = alert[1], alert[3], alert[5], alert[8], alert[9]
            dst_port = alert[4] if alert[4] is not None else 65000
            # Simply respect the source,dst format! (Correction: source is always source and dest always dest!)

            # Say 'unknown' if the port cannot be resolved
            dst_port = 'unknown' if (dst_port not in port_services.keys() or port_services[dst_port] == 'unknown') else port_services[dst_port]['name']

            if (src_ip, dst_ip) not in host_alerts.keys() and (dst_ip, src_ip) not in host_alerts.keys():
                host_alerts[(src_ip, dst_ip)] = []

            if (src_ip, dst_ip) in host_alerts.keys():
                host_alerts[(src_ip, dst_ip)].append((dst_ip, mcat, ts, dst_port, signature)) # TODO: remove the redundant host names
            else:
                host_alerts[(dst_ip, src_ip)].append((src_ip, mcat, ts, dst_port, signature))

        team_data[tid] = host_alerts.items()
    return team_data


def _get_ups_and_downs(frequencies, slopes):
    positive = [(0, slopes[0])]  # (index, slope)
    positive.extend([(i, slopes[i]) for i in range(1, len(slopes)) if (slopes[i - 1] <= 0 and slopes[i] > 0)])
    negative = [(i + 1, slopes[i + 1]) for i in range(0, len(slopes) - 1) if (slopes[i] < 0 and slopes[i + 1] >= 0)]
    if slopes[-1] < 0:  # Special case for last ramp down that's not fully gone down
        negative.append((len(slopes), slopes[-1]))
    elif slopes[-1] > 0:  # Special case for last ramp up without any ramp down
        negative.append((len(slopes), slopes[-1]))

    common = set(negative).intersection(positive)
    negative = [item for item in negative if item not in common]
    positive = [item for item in positive if item not in common]

    negative = [x for x in negative if (frequencies[x[0]] <= 0 or x[0] == len(frequencies) - 1)]
    positive = [x for x in positive if (frequencies[x[0]] <= 0 or x[0] == 0)]
    return positive, negative, common


def _plot_episodes(frequencies, episodes, mcat):
    cap = max(frequencies) + 1

    plt.figure()
    plt.title(mcat)
    plt.xlabel('Time ->')
    plt.ylabel('Frequency')
    plt.plot(frequencies, 'gray')
    for ep in episodes:
        xax_start = [ep[0]] * cap
        xax_end = [ep[1]] * cap
        yax = list(range(cap))

        plt.plot(xax_start, yax, 'g', linestyle=(0, (5, 10)))
        plt.plot(xax_end, yax, 'r', linestyle=(0, (5, 10)))

    plt.show()
    return


# Goal: (1) To first form a collective attack profile of a team
# and then (2) To compare attack profiles of teams
def _get_episodes(alert_seq, mcat, plot):
    # x-axis represents the time, y-axis represents the frequencies of alerts within a window
    dx = 0.1
    frequencies = [len(x) for x in alert_seq]

    # TODO: move these test cases into a separate test file
    # test case 1: normal sequence
    #y = [11, 0, 0, 2, 5, 2, 2, 2, 4, 2, 0, 0, 8, 6, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 1, 13, 1, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 0, 0, 9, 2]
    # test case 2: start is not detected
    #y = [ 0, 2, 145, 0, 0, 1, 101, 45, 0, 1, 18, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]
    # test case 2.5: start not detected (unfinished)
    #y = [39, 6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 12, 28, 0, 2, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4, 4, 0, 0, 1, 1, 2, 1, 2, 2, 1, 1, 1, 2, 0, 1, 2, 0, 2, 1, 1, 1, 2, 1, 1, 0, 1, 1, 1, 1]
    # test case 3: last peak not detected (unfinished)
    #y = [36, 0, 0, 0, 2, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 12, 17, 0, 0, 0, 0, 0, 0, 33, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 6, 5, 6, 1, 2, 2]
    # test case 4: last peak undetected (finished)
    #y = [1, 0, 0, 1, 3, 0, 1, 6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 2, 21, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    # test case 5: end peak is not detected
    #y = [1, 0, 0, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 3, 0, 0, 0, 0, 1, 1, 0, 0, 0, 1, 2, 0]
    # test case 6: end peak uncompleted again not detected:
    #y = [8, 4, 0, 0, 0, 4, 0, 0, 5, 0, 0, 1, 10, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 2]
    # test case 7: single peak not detected (conjoined)
    #y = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 207, 0, 53, 24, 0, 0, 0, 0, 0, 0, 0]
    # test case 8: another single peak not detected
    #y = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    # test case 9: single peak at the very end
    #y = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 294]
    # test case 10: ramp up at end
    #y = [0, 0, 0, 0, 190, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 300, 38, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 271, 272]
    #print(y)
    #y = [1, 0, 64, 2]
    #y = [2, 0, 0, 0, 0, 0, 0, 2, 3, 0, 0, 0, 0, 2, 3]
    #y = [1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]

    if sum(frequencies) == 0:
        return []
    if len(frequencies) == 1:  # Artificially augmenting list for a single action to be picked up
        frequencies = [frequencies[0], 0]

    slopes = np.diff(frequencies) / dx  # Taking derivative of frequencies
    positive, negative, common = _get_ups_and_downs(frequencies, slopes)

    if len(negative) < 1 or len(positive) < 1:
        return []

    # Get episodes (down between ups)
    episodes = []  # Tuple (start_index, end_index)
    for i in range(len(positive) - 1):
        ep1 = positive[i][0]
        ep2 = positive[i + 1][0]
        ends = []
        for j in range(len(negative)):
            if ep1 <= negative[j][0] < ep2:
                ends.append(negative[j])

        if len(ends) > 0:
            episode = (ep1, max([x[0] for x in ends]))
            episodes.append(episode)

    # Handle edge cases
    if len(positive) == 1 and len(negative) == 1:
        episode = (positive[0][0], negative[0][0])
        episodes.append(episode)

    if len(episodes) > 0 and negative[-1][0] != episodes[-1][1]:
        episode = (positive[-1][0], negative[-1][0])
        episodes.append(episode)

    if len(episodes) > 0 and positive[-1][0] != episodes[-1][0]:
        elim = [x[0] for x in common]
        if len(elim) > 0 and max(elim) > positive[-1][0]:
            episode = (positive[-1][0], max(elim))
            episodes.append(episode)

    if len(episodes) == 0 and len(positive) == 2 and len(negative) == 1:
        episode = (positive[1][0], negative[0][0])
        episodes.append(episode)

    if plot:
        _plot_episodes(frequencies, episodes, mcat)

    return episodes


def _create_episode(alert_seq_epi, mcat, tid):
    # Flatten relevant data from the windows of the corresponding alert sequence
    services = [alert[3] for window in alert_seq_epi for alert in window]
    unique_signatures = list(set([alert[4] for window in alert_seq_epi for alert in window]))
    events = [len(window) for window in alert_seq_epi]
    alert_volume = round(sum(events) / float(len(events)), 1)

    # Make exact start/end times based on alert timestamps
    timestamps = [alert[2] for window in alert_seq_epi for alert in window]
    first_ts, last_ts = min(timestamps), max(timestamps)

    # Make the start/end times the actual elapsed times
    start_time = (first_ts - start_times[tid]).total_seconds()
    end_time = (last_ts - start_times[tid]).total_seconds()
    period = end_time - start_time

    episode = (start_time, end_time, mcat, len(events), alert_volume, period, services, unique_signatures, (first_ts, last_ts))
    return episode


def _legend_without_duplicate_labels(ax, fontsize=10, loc='upper right'):
    handles, labels = ax.get_legend_handles_labels()
    unique = [(h, l) for i, (h, l) in enumerate(zip(handles, labels)) if l not in labels[:i]]
    unique = sorted(unique, key=lambda x: x[1])
    ax.legend(*zip(*unique), loc=loc, fontsize=fontsize)


def _plot_alert_volume_per_episode(tid, attacker_victim, host_episodes, mcats):
    plt.figure(figsize=(10, 10))
    ax = plt.gca()
    plt.title('Micro attack episodes | Team: ' + str(tid) + ' | Host: ' + '->'.join(attacker_victim))
    plt.xlabel('Time Window (sec)')
    plt.ylabel('Micro attack stages')
    # NOTE: Line thicknesses are on per-host basis
    tmax = max([epi[4] for epi in host_episodes])
    tmin = min([epi[4] for epi in host_episodes])
    for idx, ep in enumerate(host_episodes):
        xax = list(np.arange(ep[0], ep[1] + 1))
        yax = [mcats.index(ep[2])] * len(xax)
        thickness = ep[4]
        lsize = ((thickness - tmin) / (tmax - tmin)) * (5 - 0.5) + 0.5 if (tmax - tmin) != 0.0 else 0.5
        # lsize = np.log(thickness) + 1 TODO: Either take log or normalize between [0.5 5]
        msize = (lsize * 2) + 1
        ax.plot(xax, yax, color=mcols[macro_inv[micro2macro[micro[ep[2]]]]], linewidth=lsize)
        ax.plot(ep[0], mcats.index(ep[2]), color=mcols[macro_inv[micro2macro[micro[ep[2]]]]], marker='.', linewidth=0,
                markersize=msize, label=micro2macro[micro[ep[2]]])
        ax.plot(ep[1], mcats.index(ep[2]), color=mcols[macro_inv[micro2macro[micro[ep[2]]]]], marker='.', linewidth=0,
                markersize=msize)
        plt.yticks(range(len(mcats)), [x.split('.')[1] for x in micro.values()], rotation=0)
    _legend_without_duplicate_labels(ax)
    plt.grid(True, alpha=0.4)

    # plt.tight_layout()
    # plt.savefig('Pres-Micro-attack-episodes-Team'+str(tid) +'-Connection'+ attacker[0]+'--'+attacker[1]+'.png')
    plt.show()


# Step 2: Create alert sequence and get episodes
def aggregate_into_episodes(_team_alerts, step=150):
    PRINT = False

    team_data = _group_alerts_per_team(_team_alerts)
    _team_episodes = []
    team_times = []

    print('---------------- TEAMS -------------------------')

    mcats = list(micro.keys())
    for tid, team in team_data.items():
        print(tid, sep=' ', end=' ', flush=True)
        team_host_episodes = dict()
        _team_times = dict()
        for attacker_victim, alerts in team:
            if len(alerts) <= 1:
                continue

            # Alert format: (dst_ip, mcat, ts, dst_port, signature)
            # print(attacker_victim, len([(x[1]) for x in alerts])) # TODO: what about IPs that are not attacker related?
            first_elapsed_time = round((alerts[0][2] - start_times[tid]).total_seconds(), 2)

            _team_times['->'.join(attacker_victim)] = first_elapsed_time

            ts = [x[2] for x in alerts]
            diff_ts = [0.0]
            for i in range(1, len(ts)):
                diff_ts.append(round((ts[i] - ts[i - 1]).total_seconds(), 2))
            elapsed_time = list(accumulate(diff_ts))
            relative_elapsed_time = [round(x + first_elapsed_time, 2) for x in elapsed_time]

            host_episodes = []
            for mcat in mcats:
                # 2.5-minute (150s) fixed step (window). Can be reduced or increased depending on required granularity
                alert_seq = []
                for i in range(int(first_elapsed_time), int(relative_elapsed_time[-1]), step):
                    window = [a for dt, a in zip(relative_elapsed_time, alerts) if (i <= dt < (i + step)) and a[1] == mcat]
                    alert_seq.append(window)  # Alerts per 'step' seconds (window)

                raw_episodes = _get_episodes(alert_seq, micro[mcat], plot=False)
                if len(raw_episodes) > 0:
                    for epi in raw_episodes:
                        alert_seq_epi = alert_seq[epi[0]:epi[1]+1]
                        episode = _create_episode(alert_seq_epi, mcat, tid)
                        host_episodes.append(episode)

            if len(host_episodes) == 0:
                continue

            host_episodes.sort(key=lambda tup: tup[0])
            team_host_episodes[attacker_victim] = host_episodes

            if PRINT:
                _plot_alert_volume_per_episode(tid, attacker_victim, host_episodes, mcats)

        _team_episodes.append(team_host_episodes)
        team_times.append(_team_times)
    return _team_episodes, team_times


# Step 3: Create episode sequences
# Host = [connections] instead of team level representation
def host_episode_sequences(_team_episodes):
    _host_data = {}
    print('# teams:', len(_team_episodes))
    print('----- TEAMS -----')
    for tid, team in enumerate(_team_episodes):
        print(tid, sep=' ', end=' ', flush=True)
        for (attacker, victim), episodes in team.items():
            if len(episodes) < 2:
                continue
            # if ('10.0.0' in attacker or '10.0.1' in attacker):
            #        continue

            att = 't' + str(tid) + '-' + attacker
            if att not in _host_data.keys():
                _host_data[att] = []

            extended_episode = [epi + (victim,) for epi in episodes]

            _host_data[att].append(extended_episode)
            _host_data[att].sort(key=lambda tup: tup[0][0])

    print('\n# episode sequences:', len(_host_data))
    return _host_data


# Step 4.1: Split episode sequences for an attacker-victim pair into episode subsequences.
# Each episode subsequence represents an attack attempt.
def break_into_subbehaviors(_host_data):
    _subsequences = dict()
    cut_length = 4
    FULL_SEQ = False

    print('----- Sub-sequences -----')
    for i, (attacker, victim_episodes) in enumerate(_host_data.items()):
        print((i + 1), sep=' ', end=' ', flush=True)
        for episodes in victim_episodes:
            if len(episodes) < 2:
                continue

            victim = episodes[0][-1]
            attacker_victim = attacker + '->' + victim
            pieces = math.floor(len(episodes) / cut_length)
            if FULL_SEQ:
                _subsequences[attacker_victim] = episodes
                continue
            if pieces < 1:
                _subsequences[attacker_victim + '-0'] = episodes
                continue

            # Cut episode sequence when a low-severity episode follows a high-severity episode
            count = 0
            mcats = [epi[2] for epi in episodes]
            cuts = [i for i in range(len(episodes) - 1) if (len(str(mcats[i])) > len(str(mcats[i + 1])))]  # (ep[i] > 100 and ep[i+1] < 10)]

            rest = (0, len(episodes) - 1)
            for j in range(len(cuts)):
                start = 0 if j == 0 else cuts[j - 1] + 1
                end = cuts[j]
                rest = (end + 1, len(episodes) - 1)
                subsequence = episodes[start:end+1]
                if len(subsequence) < 2:
                    continue
                _subsequences[attacker_victim + '-' + str(count)] = subsequence
                count += 1
            subsequence = episodes[rest[0]:rest[1]+1]
            if len(subsequence) < 2:
                # print('discarding symbol ', [x[2] for x in al]) # TODO This one is not cool1
                continue
            _subsequences[attacker_victim + '-' + str(count)] = subsequence

    print('\n# sub-sequences:', len(_subsequences))
    return _subsequences


# Step 4.2: Generate traces for FlexFringe (27 Aug 2020)
def generate_traces(subsequences, datafile):
    all_services = [[_most_frequent(epi[6]) for epi in subseq] for subseq in subsequences.values()]
    print('----- All unique services -----')
    print(set([service for services_subseq in all_services for service in services_subseq]))
    print('---- end ----- ')

    num_traces = 0
    unique_mcat_mserv = set()  # FlexFringe treats the (mcat,mserv) pairs as symbols of the alphabet

    episode_traces = []
    for i, episodes in enumerate(subsequences.values()):
        if len(episodes) < 3:
            continue
        num_traces += 1
        mcats = [x[2] for x in episodes]
        num_services = [len(set((x[6]))) for x in episodes]
        max_services = [_most_frequent(x[6]) for x in episodes]
        stime = [x[0] for x in episodes]

        #multi = [str(c) + ":" + str(n) + "," + str(s) for (c, s, n) in zip(mcats, max_services, num_services)] # multivariate case
        mcat_mserv_pairs = [small_mapping[mcat] + "|" + mserv for mcat, mserv in zip(mcats, max_services)]
        unique_mcat_mserv.update(mcat_mserv_pairs)
        mcat_mserv_pairs.reverse()  # Reverse traces to accentuate high-severity episodes (to create an S-PDFA)
        trace = '1' + " " + str(len(mcats)) + ' ' + ' '.join(mcat_mserv_pairs) + '\n'
        episode_traces.append(trace)

    f = open(datafile, 'w')
    f.write(str(num_traces) + ' ' + str(len(unique_mcat_mserv)) + '\n')
    for trace in episode_traces:
        f.write(trace)
    f.close()
    print('\n# episode traces:', len(episode_traces))


# Step 5: Learn the S-PDFA model (2 sept 2020)
def flexfringe(*args, **kwargs):
    """Wrapper to call the flexfringe binary

    Keyword arguments:
    position 0 -- input file with trace samples
    kwargs -- list of key=value arguments to pass as command line arguments
    """

    command = []
    if len(kwargs) == 1:
        command = ["--help"]
    for key in kwargs:
        command += ["--" + key + "=" + kwargs[key]]

    result = subprocess.run(["FlexFringe/flexfringe"] + command + [args[0]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    print(result.returncode, result.stdout, result.stderr)


def load_model(model_file):
    """Wrapper to load resulting model json file

    Keyword arguments:
    model_file -- path to the json model file
    """

    # Because users can provide unescaped new lines breaking json conventions
    #   in the labels, we are removing them from the label fields
    with open(model_file) as fh:
        model_data = fh.read()
        model_data = re.sub(r'\"label\" : \"([^\n|]*)\n([^\n]*)\"', r'"label" : "\1 \2"', model_data)

    model_data = model_data.replace('\n', '').replace(',,', ',')
    model_data = re.sub(',+', ',', model_data)
    machine = json.loads(model_data)

    dfa = defaultdict(lambda: defaultdict(str))
    for edge in machine["edges"]:
        dfa[edge["source"]][edge["name"]] = edge["target"]

    # If you want to add some properties of the nodes, uncomment the following lines and add the properties you need
    # for entry in machine["nodes"]:
    #    dfa[str(entry['id'])]["isred"] = int(entry['isred'])

    return dfa


def traverse(dfa, sinks, sequence):
    """Wrapper to traverse a given model with a string to create a state subsequence.

    Keyword arguments:
    dfa -- loaded main model
    sinks -- loaded sinks model
    sequence -- space-separated string to accept/reject in dfa
    """
    sev_sinks = set()
    state = "0"
    state_list = ["0"]
    for event in sequence.split(" "):
        sym = event.split(":")[0]  # TODO: remove in case multi is removed in `generate_traces` method
        sev = rev_smallmapping[sym.split('|')[0]]
        state = dfa[state][sym]
        if state == "":
            # Only keep IDs of medium- and high-severity sinks
            if len(str(sev)) >= 2:
                if state_list[-1] in sinks and sym in sinks[state_list[-1]]:
                    state = sinks[state_list[-1]][sym]
                else:
                    state = '-1'  # With `printblue = 1` in spdfa-config.ini this should not happen
            else:
                state = '-1'

        state_list.append(state)
        if state in sinks and len(str(sev)) >= 2:
            sev_sinks.add(state)

    return state_list, sev_sinks


def encode_sequences(dfa, sinks):
    traces = []
    with open(path_to_traces) as tf:
        lines = tf.readlines()[1:]

    for line in lines:  # TODO: reuse the traces created in generate_traces method
        parts = line.strip('\n').split(' ')
        trace = ' '.join(parts[2:])
        traces.append(trace)

    traces_in_sinks, total_traces = 0, 0
    state_traces = dict()
    med_sev_states, high_sev_states, sev_sinks = set(), set(), set()
    for i, sample in enumerate(traces):
        state_list, _sev_sinks = traverse(dfa, sinks, sample)
        state_traces[i] = state_list

        total_traces += len(state_list)  # TODO: decide what to do with the root
        traces_in_sinks += state_list.count('-1')  # Add low-severity sinks (i.e. stateID = -1)
        traces_in_sinks += len(_sev_sinks)         # Add medium- and high-severity sinks (i.e. stateID != -1)

        assert (len(sample.split(' ')) + 1 == len(state_traces[i]))

        state_list = state_list[1:]
        sample = sample.split(' ')
        med_sev = [int(state) for sym, state in zip(sample, state_list) if len(str(rev_smallmapping[sym.split('|')[0]])) == 2]  # TODO: add a method for low-, med- and high-sev
        med_sev_states.update(med_sev)
        high_sev = [int(state) for sym, state in zip(sample, state_list) if len(str(rev_smallmapping[sym.split('|')[0]])) == 3]
        high_sev_states.update(high_sev)
        sev_sinks.update(_sev_sinks)

    print('Traces in sinks:', traces_in_sinks, 'Total traces:', total_traces, 'Percentage:', 100 * (traces_in_sinks / float(total_traces)))
    print('Total medium-severity states:', len(med_sev_states))
    print('Total high-severity states:', len(high_sev_states))
    print('Total severe sinks:', len(sev_sinks))
    return state_traces, med_sev_states, high_sev_states, sev_sinks


# Collecting sub-behaviors back into the same trace -- state_sequences is the new object to deal with
def make_state_sequences(episode_subsequences, state_traces, med_states, sev_states):
    level_one = set()  # TODO: what to do with these states?
    state_sequences = dict()
    counter = -1
    for tid, (attack, episode_subsequence) in enumerate(episode_subsequences.items()):

        if len(episode_subsequence) < 3:
            continue
        counter += 1
        
        if '10.0.254' not in attack:  # TODO: this part has to be commented for CCDC dataset
            continue
        if '147.75' in attack or '69.172' in attack:
            continue

        trace = [int(state) for state in state_traces[counter]]
        max_services = [_most_frequent(epi[6]) for epi in episode_subsequence]  # TODO: compute mserv once (above)

        if 0 in trace and (not set(trace).isdisjoint(sev_states) or not set(trace).isdisjoint(med_states)):
            level_one.add(trace[1])

        trace = trace[1:][::-1]  # Reverse the trace from the S-PDFA back
        
        # start_time, end_time, mcat, state_ID, mserv, list of unique signatures, (1st and last timestamp)
        state_subsequence = [(epi[0], epi[1], epi[2], trace[i], max_services[i], epi[7], epi[8])
                                for i, epi in enumerate(episode_subsequence)]
        
        parts = attack.split('->')
        team, attacker = parts[0].split('-')
        victim, attack_num = parts[1].split('-')
        attacker_victim = team + '-' + attacker + '->' + victim
        attacker_victim_inv = team + '-' + victim + '->' + attacker
        inv = False
        if '10.0.254' in victim:
            inv = True
        
        if attacker_victim not in state_sequences.keys() and attacker_victim_inv not in state_sequences.keys():
            if inv:
                state_sequences[attacker_victim_inv] = []
            else:
                state_sequences[attacker_victim] = []
        if inv:
            state_sequences[attacker_victim_inv].extend(state_subsequence)
            state_sequences[attacker_victim_inv].sort(key=lambda epi: epi[0])  # Sort in place based on starting times
        else:
            state_sequences[attacker_victim].extend(state_subsequence)
            state_sequences[attacker_victim].sort(key=lambda epi: epi[0])  # Sort in place based on starting times
        
    # print('High-severity objective states', level_one, len(level_one))
    return state_sequences
    

def make_state_groups(state_sequences, data_file):
    state_groups = dict()
    all_states = set()
    gcols = ['lemonchiffon', 'gold', 'khaki', 'darkkhaki', 'beige', 'goldenrod', 'wheat', 'papayawhip', 'orange', 'oldlace', 'bisque']
    for _, episodes in state_sequences.items():
        states = [(epi[2], epi[3]) for epi in episodes]
        all_states.update([epi[3] for epi in episodes])

        for i, state in enumerate(states):
            macro = micro2macro[micro[state[0]]].split('.')[1]
            if state[1] == -1 or state[1] == 0:  # Skip the root node and nodes with ID -1
                continue
            if macro not in state_groups.keys():
                state_groups[macro] = set()
            state_groups[macro].add(state[1])

    with open(data_file + ".ff.final.dot", 'r') as model_file:
        model_lines = model_file.readlines()
    written = []
    outlines = ['digraph modifiedDFA {\n']
    for gid, (group, states) in enumerate(state_groups.items()):
        print(group)
        outlines.append('subgraph cluster_' + group + ' {\n')
        outlines.append('style=filled;\n')
        outlines.append('color=' + gcols[gid] + ';\n')
        outlines.append('label = "' + group + '";\n')
        for i, line in enumerate(model_lines):
            node_line = re.match(r'\D+(\d+)\s\[\slabel="\d.*', line)
            if node_line:
                node = int(node_line.group(1))
                if node in states:
                    c = i
                    while '];' not in model_lines[c]:
                        outlines.append(model_lines[c])
                        written.append(c)
                        c += 1
                    outlines.append(model_lines[c])
                    written.append(c)
                elif node not in all_states and group == 'ACTIVE_RECON':
                    if node != 0:
                        c = i
                        while '];' not in model_lines[c]:
                            outlines.append(model_lines[c])
                            written.append(c)
                            c += 1
                        outlines.append(model_lines[c])
                        written.append(c)
                        state_groups['ACTIVE_RECON'].add(node)
                    print('ERROR: manually handled', node, ' in ACTIVE_RECON')  # TODO: include edges or not?
            '''edge_line = re.match(r'\D+(\d+)\s->\s(\d+)\s\[label=.*', line)  # 0 -> 1 [label=
            if edge_line:
                node = int(edge_line.group(1))
                if node in states:
                    c = i
                    while '];' not in model_lines[c]:
                        outlines.append(model_lines[c])
                        written.append(c)
                        c += 1
                    outlines.append(model_lines[c])
                    written.append(c)'''
        outlines.append('}\n')

    for i, line in enumerate(model_lines):
        if i < 2:
            continue
        if i not in written:
            outlines.append(line)

    filename = 'spdfa-clustered-' + data_file + '-dfa'
    with open(filename + '.dot', 'w') as outfile:
        for line in outlines:
            outfile.write(line)

    os.system("dot -Tpng " + filename + ".dot -o " + filename + ".png")
    return state_groups


def group_episodes_per_av(state_sequences):
    # Experiment: attack graph for one victim w.r.t time
    victim_episodes = dict()  # Episodes per (team, victim)
    for attack, episodes in state_sequences.items():
        team = attack.split('-')[0]
        victim = attack.split('->')[1]
        team_victim = team + '-' + victim
        if team_victim not in victim_episodes.keys():
            victim_episodes[team_victim] = []
        victim_episodes[team_victim].extend(episodes)
        victim_episodes[team_victim] = sorted(victim_episodes[team_victim], key=lambda epi: epi[0])  # By start time
    # Sort by start time across all
    victim_episodes = {k: v for k, v in sorted(victim_episodes.items(), key=lambda kv: len([epi[0] for epi in kv[1]]))}
    print('Victims hosts: ', set([team_victim.split('-')[-1] for team_victim in victim_episodes.keys()]))

    attacker_episodes = dict()  # Episodes per (team, attacker)
    for attack, episodes in state_sequences.items():
        team = attack.split('-')[0]
        attacker = (attack.split('->')[0]).split('-')[1]
        team_attacker = team + '-' + attacker
        if team_attacker not in attacker_episodes.keys():
            attacker_episodes[team_attacker] = []
        attacker_episodes[team_attacker].extend(episodes)
        attacker_episodes[team_attacker] = sorted(attacker_episodes[team_attacker], key=lambda epi: epi[0])
    print('Attacker hosts: ', set([team_attacker.split('-')[1] for team_attacker in attacker_episodes.keys()]))

    return attacker_episodes, victim_episodes


# Translate technical nodes to human-readable
def translate(label, root=False):
    new_label = ""
    parts = label.split("|")
    if root:
        new_label += 'Victim: ' + str(root) + '\n'

    if len(parts) >= 1:
        new_label += verbose_micro[parts[0]]
    if len(parts) >= 2:
        new_label += "\n" + parts[1]
    if len(parts) >= 3:
        new_label += " | ID: " + parts[2]

    return new_label
    
## Per-objective attack graph for dot: 14 Nov (final attack graph) 
def make_AG(condensed_v_data, condensed_data, state_groups, sev_sinks, datafile, expname):  
    tcols = {
        't0': 'maroon',
        't1': 'orange',
        't2': 'darkgreen',
        't3': 'blue',
        't4': 'magenta',
        't5': 'purple',
        't6': 'brown',
        't7': 'tomato',
        't8': 'turquoise',
        't9': 'skyblue',
        
    }
    if SAVE:
        try:
            #if path.exists('AGs'):
            #    shutil.rmtree('AGs')
            dirname = expname+'AGs'
            os.mkdir(dirname)
        except:
            print("Can't create directory here")
        else:
            print("Successfully created directory for AGs")
    
    
    
    

    shapes = ['oval', 'oval', 'oval', 'box', 'box', 'box', 'box', 'hexagon', 'hexagon', 'hexagon', 'hexagon', 'hexagon']
    in_main_model = [[episode[3] for episode in sequence] for sequence in condensed_data.values()] # all IDs in the main model (including high-sev sinks)
    in_main_model = set([item for sublist in in_main_model for item in sublist])
    
    ser_total = dict()
    simple = dict()
    total_victims = set([x.split('-')[1] for x in list(condensed_v_data.keys())]) # collect all victim IPs
    
    OBJ_ONLY = False # Experiment 1: mas+service or only mas?
    attacks = set()
    for episodes in condensed_data.values(): # iterate over all episodes and collect the objective nodes.
        for ep in episodes: # iterate over every episode
            if len(str(ep[2])) == 3: # If high-seveity, then include it
                cat = micro[ep[2]].split('.')[1]
                vert_name = None
                if OBJ_ONLY:
                    vert_name = cat
                else:
                    vert_name = cat+'|'+ep[4] # cat + service
                attacks.add(vert_name)
    attacks = list(attacks)
        
    for int_victim in total_victims:  # iterate over every victim
        print('\n!!! Rendering AGs for Victim ', int_victim,'\n',  sep=' ', end=' ', flush=True)
        for attack in attacks: # iterate over every attack
            print('\t!!!! Objective ', attack,'\n',  sep=' ', end=' ', flush=True)
            collect = dict()
            
            team_level = dict()
            observed_obj = set() # variants of current objective
            nodes = {}
            vertices, edges = 0, 0
            for att,episodes in condensed_data.items(): # iterate over (a,v): [episode, episode, episode]
                if int_victim != att.split("->")[1]: # if it's not the right victim, then don't process further
                    continue
                vname_time = []
                for ep in episodes:
                    start_time = round(ep[0]/1.0)
                    end_time = round(ep[1]/1.0)
                    cat = micro[ep[2]].split('.')[1]
                    signs = ep[5]
                    timestamps = ep[6]
                    stateID = -1
                    if ep[3] in in_main_model:
                        stateID = '' if len(str(ep[2])) == 1 else '|'+str(ep[3])
                    else:
                        stateID = '|Sink'
                    
                    vert_name = cat + '|'+ ep[4] + stateID
                    
                    vname_time.append((vert_name, start_time, end_time, signs, timestamps))
                    
                if not sum([True if attack in x[0] else False for x in vname_time]): # if the objective is never reached, don't process further
                    continue
                    
                # if it's an episode sequence targetting the requested victim and obtaining the requested objective,
                attempts = []
                sub_attempt = []
                for (vname, start_time, end_time, signs, ts) in vname_time: # cut each attempt until the requested objective
                    sub_attempt.append((vname, start_time, end_time, signs, ts)) # add the vertex in path
                    if attack.split("|")[:2] == vname.split("|")[:2]: # if it's the objective
                        if len(sub_attempt) <= 1: ## If only a single node, reject
                            sub_attempt = []
                            continue
                        attempts.append(sub_attempt)
                        sub_attempt = []
                        observed_obj.add(vname)
                        continue
                team_attacker = att.split('->')[0] # team+attacker
                if team_attacker not in team_level.keys():
                    team_level[team_attacker] = []
                    
                team_level[team_attacker].extend(attempts)
                #team_level[team_attacker] = sorted(team_level[team_attacker], key=lambda item: item[1])
            #print(observed_obj)
            # print('elements in graph', team_level.keys(), sum([len(x) for x in team_level.values()]))

            if sum([len(x) for x in team_level.values()]) == 0: # if no team obtains this objective or targets this victim, don't generate its AG.
                continue

            AGname = attack.replace('|', '').replace('_','').replace('-','').replace('(','').replace(')', '')
            lines = []
            lines.append((0,'digraph '+ AGname + ' {'))
            lines.append((0,'rankdir="BT"; \n graph [ nodesep="0.1", ranksep="0.02"] \n node [ fontname=Arial, fontsize=24,penwidth=3]; \n edge [ fontname=Arial, fontsize=20,penwidth=5 ];'))
            root_node = translate(attack, root=int_victim)
            lines.append((0, '"'+root_node+'" [shape=doubleoctagon, style=filled, fillcolor=salmon];'))
            lines.append((0, '{ rank = max; "'+root_node+'"}'))
            
            for obj in list(observed_obj): # for each variant of objective, add a link to the root node, and determine if it's sink
                lines.append((0,'"'+translate(obj)+'" -> "'+root_node+'"'))
                
                sinkflag = False
                for sink in sev_sinks:    
                    if obj.split("|")[-1] == sink:
                        sinkflag = True
                        break
                if sinkflag:
                    lines.append((0,'"'+translate(obj)+'" [style="filled,dotted", fillcolor= salmon]'))
                else:
                    lines.append((0,'"'+translate(obj)+'" [style=filled, fillcolor= salmon]'))
            
            samerank = '{ rank=same; "'+ '" "'.join([translate(x) for x in observed_obj]) # all obj variants have the same rank
            samerank += '"}'
            lines.append((0,samerank))


            already_addressed = set()
            for attackerID,attempts in team_level.items(): # for every attacker that obtains this objective
                color = tcols[attackerID.split('-')[0]] # team color
                ones = [''.join([action[0] for action in attempt]) for attempt in attempts]
                unique = len(set(ones)) # count exactly unique attempts
                #print(unique)
                #print('team', attackerID, 'total paths', len(attempts), 'unique paths', unique, 'longest path:', max([len(x) for x in attempts]), \
                #     'shortest path:', min([len(x) for x in attempts]))
                
                #path_info[attack][attackerID].append((len(attempts), unique, max([len(x) for x in attempts]), min([len(x) for x in attempts])))
                
                for attempt in attempts: # iterate over each attempt
                    # record all nodes
                    for action in attempt:
                        if action[0] not in nodes.keys():
                            nodes[action[0]] = set()
                        nodes[action[0]].update(action[3])
                    # nodes
                    for vid,(vname,start_time,end_time,signs,_) in enumerate(attempt): # iterate over each action in an attempt
                        if vid == 0: # if first action
                            if 'Sink' in vname: # if sink, make dotted
                                lines.append((0,'"'+translate(vname)+'" [style="dotted,filled", fillcolor= yellow]'))
                            else:
                                sinkflag = False
                                for sink in sev_sinks:    
                                    if vname.split("|")[-1] == sink: # else if a high-sev sink, make dotted too
                                        sinkflag = True
                                        break
                                if sinkflag:
                                    lines.append((0,'"'+translate(vname)+'" [style="dotted,filled", fillcolor= yellow]'))
                                    already_addressed.add(vname.split('|')[2])
                                else: # else, normal starting node
                                    lines.append((0,'"'+translate(vname)+'" [style=filled, fillcolor= yellow]'))
                        else: # for other actions
                            if 'Sink' in vname: # if sink
                                line = [x[1] for x in lines] # take all AG graph lines so far, and see if it was ever defined before, re-define it to be dotted
                                quit = False
                                for l in line:
                                    if (translate(vname) in l) and ('dotted' in l) and ('->' not in l): # if already defined as dotted, move on
                                        quit = True
                                        break
                                if quit:
                                    continue
                                partial = '"'+translate(vname)+'" [style="dotted' # redefine here
                                if not sum([True if partial in x else False for x in line]):
                                    lines.append((0,partial+'"]'))

                    # transitions
                    bi = zip(attempt, attempt[1:]) # make bigrams (sliding window of 2)
                    for vid,((vname1,time1,etime1, signs1, ts1),(vname2,_,_, signs2, ts2)) in enumerate(bi): # for every bigram
                        _from_last = ts1[1].strftime("%d/%m/%y, %H:%M:%S")
                        _to_first = ts2[0].strftime("%d/%m/%y, %H:%M:%S")
                        gap = round((ts2[0] - ts1[1]).total_seconds())
                        if vid == 0:  # first transition, add attacker IP
                            lines.append((time1, '"' + translate(vname1) + '"' + ' -> ' + '"' + translate(vname2) +
                                          '" [ color=' + color + '] ' + '[label=<<font color="' + color + '"> start_next: ' + _to_first + '<br/>gap: ' +
                                          str(gap) + 'sec<br/>end_prev: ' + _from_last + '</font><br/><font color="' + color + '"><b>Attacker: ' +
                                          attackerID.split('-')[1] + '</b></font>>]'
                                          ))
                        else:
                            lines.append((time1, '"' + translate(vname1) + '"' + ' -> ' + '"' + translate(vname2) +
                                          '"' + ' [ label="start_next: ' + _to_first + '\ngap: ' +
                                          str(gap) + 'sec\nend_prev: ' + _from_last + '"]' + '[ fontcolor="' + color + '" color=' + color + ']'
                                          ))

            for vname, signatures in nodes.items(): # Go over all vertices again and define their shapes + make high-sev sink states dotted
                mas = vname.split('|')[0]
                mas = macro_inv[micro2macro['MicroAttackStage.'+mas]]
                shape = shapes[mas]
                if shape == shapes[0] or vname.split('|')[2] in already_addressed: # if it's oval, we dont do anything because its not high-sev sink
                    lines.append((0,'"'+translate(vname)+'" [shape='+shape+']'))
                else:
                    sinkflag = False
                    for sink in sev_sinks:    
                        if vname.split("|")[-1] == sink:
                            sinkflag = True
                            break
                    if sinkflag:
                        lines.append((0,'"'+translate(vname)+'" [style="dotted", shape='+shape+']'))
                    else:
                        lines.append((0,'"'+translate(vname)+'" [shape='+shape+']'))
                # add tooltip
                lines.append((1, '"'+translate(vname)+'"'+' [tooltip="'+ "\n".join(signatures) +'"]'))
            lines.append((1000,'}'))
            
            for l in lines: # count vertices and edges
                if '->' in l[1]:
                    edges +=1
                elif 'shape=' in l[1]:
                    vertices +=1
            simple[int_victim+'-'+AGname] = (vertices, edges)
            
            #print('# vert', vertices, '# edges: ', edges,  'simplicity', vertices/float(edges))
            if SAVE:
                out_f_name = datafile+'-attack-graph-for-victim-'+int_victim+'-'+AGname 
                f = open(dirname+'/'+ out_f_name +'.dot', 'w')
                for l in lines:
                    f.write(l[1])
                    f.write('\n')
                f.close()
                
                os.system("dot -Tpng "+dirname+'/'+out_f_name+".dot -o "+dirname+'/'+out_f_name+".png")
                os.system("dot -Tsvg "+dirname+'/'+out_f_name+".dot -o "+dirname+'/'+out_f_name+".svg")
                if DOCKER:
                    os.system("rm "+dirname+'/'+out_f_name+".dot")
                #print('~~~~~~~~~~~~~~~~~~~~saved')
            print('#', sep=' ', end=' ', flush=True)
        #print('total high-sev states:', len(path_info))
        #path_info = dict(sorted(path_info.items(), key=lambda kv: kv[0]))
        #for attackerID,v in path_info.items():
        #    print(attackerID)
        #    for t,val in v.items():
        #       print(t, val)
    #for attackerID,v in ser_total.items():
    #    print(attackerID, len(v), set([x.split('|')[0] for x in v]))


# ----- MAIN ------

if len(sys.argv) < 5:
    print('Usage: sage.py {path/to/json/files} {experiment_name} {alert_filtering_window (def=1.0)} {alert_aggr_window (def=150)} {(start_hour,end_hour)[Optional]}')
    sys.exit()

path_to_json_files = sys.argv[1]
experiment_name = sys.argv[2]
alert_filtering_window = float(sys.argv[3])
alert_aggr_window = int(sys.argv[4])
start_hour, end_hour = 0, 100
if len(sys.argv) > 5:
    try:
        start_hour = float(sys.argv[5])
        end_hour = float(sys.argv[6])
        print('Filtering alerts. Only parsing from %d-th to %d-th hour (relative to the start of the alert capture)' % (start_hour, end_hour))
    except (ValueError, TypeError):
        print('Error parsing hour filter range')
        sys.exit()

# We cheat a bit: In case user filters to only see alerts from (s,e) range,
#   we record the first alert just to get the real time-elapsed since first alert
start_times = []

path_to_ini = "FlexFringe/ini/spdfa-config.ini"

path_to_traces = experiment_name + '.txt'

print('------ Downloading the IANA port-service mapping ------')
port_services = load_iana_mapping()

print('------ Reading alerts ------')
(team_alerts, team_labels) = load_data(path_to_json_files, alert_filtering_window)
plot_histogram(team_alerts, team_labels)

print('------ Converting to episodes ------')
team_episodes, _ = aggregate_into_episodes(team_alerts, step=alert_aggr_window)

print('\n------ Converting to episode sequences ------')
host_data = host_episode_sequences(team_episodes)

print('------ Breaking into sub-sequences and generating traces ------')
episode_subsequences = break_into_subbehaviors(host_data)
generate_traces(episode_subsequences, path_to_traces)


print('------ Learning S-PDFA ------')
flexfringe(path_to_traces, ini=path_to_ini, symbol_count="2", state_count="4")

os.system("dot -Tpng " + path_to_traces + ".ff.final.dot -o " + path_to_traces + ".png")

print('------ !! Special: Fixing syntax error in main model and sink files ------')
print('--- Sinks')
with open(path_to_traces + ".ff.finalsinks.json", 'r') as file:
    filedata = file.read()
stripped = re.sub(r'[\s+]', '', filedata)
extra_commas = re.search(r'(}(,+)]}$)', stripped)
if extra_commas is not None:
    comma_count = (extra_commas.group(0)).count(',')
    print(extra_commas.group(0), comma_count)
    filedata = ''.join(filedata.rsplit(',', comma_count))
    with open(path_to_traces + ".ff.finalsinks.json", 'w') as file:
        file.write(filedata)

print('--- Main')
with open(path_to_traces + ".ff.final.json", 'r') as file:
    filedata = file.read()
stripped = re.sub(r'[\s+]', '', filedata)
extra_commas = re.search(r'(}(,+)]}$)', stripped)
if extra_commas is not None:
    comma_count = (extra_commas.group(0)).count(',')
    print(extra_commas.group(0), comma_count)
    filedata = ''.join(filedata.rsplit(',', comma_count))
    with open(path_to_traces + ".ff.final.json", 'w') as file:
        file.write(filedata)

print('------ Loading and traversing S-PDFA ------')
main_model = load_model(path_to_traces + ".ff.final.json")
sinks_model = load_model(path_to_traces + ".ff.finalsinks.json")

print('------ Encoding into state sequences ------')
# Encoding traces into state sequences
state_traces, med_sev_states, high_sev_states, severe_sinks = encode_sequences(main_model, sinks_model)
state_sequences = make_state_sequences(episode_subsequences, state_traces, med_sev_states, high_sev_states)

print('------ Clustering state groups ------')
state_groups = make_state_groups(state_sequences, path_to_traces)

print('------ Grouping episodes per (team, victim) ------')
episodes_per_attacker, episodes_per_victim = group_episodes_per_av(state_sequences)

print('------ Making alert-driven AGs ------')
make_AG(episodes_per_victim, state_sequences, state_groups, severe_sinks, path_to_traces, experiment_name)

if DOCKER:
    print('Deleting extra files')
    os.system("rm " + path_to_traces + ".ff.final.dot")
    os.system("rm " + path_to_traces + ".ff.final.json")
    os.system("rm " + path_to_traces + ".ff.finalsinks.json")
    os.system("rm " + path_to_traces + ".ff.finalsinks.dot")
    os.system("rm " + path_to_traces + ".ff.init.dot")
    os.system("rm " + path_to_traces + ".ff.init.json")
    os.system("rm " + path_to_traces + ".ff.initsinks.dot")
    os.system("rm " + path_to_traces + ".ff.initsinks.json")
    os.system("rm " + "spdfa-clustered-" + path_to_traces + "-dfa.dot")

print('\n------- FIN -------')
# ----- END MAIN ------
