"""Room tidiness, independent of robot cleaning history and reward points."""


def space_states(spaces, board, active, paused, events, now):
    ready = {}
    changed = {}
    for event in events:
        if event.get('type') == 'vacuum_ready':
            for sid in event['spaces']:
                ready[sid] = max(ready.get(sid, 0), event['ts'])
        elif event.get('type') == 'reopen':
            changed[event['chore']] = event['ts']
    result = {}
    for space in spaces:
        key = space['id']
        jobs = space.get('chores', [])
        row = {k: space[k] for k in ('id', 'name', 'floor', 'map_rooms', 'chores') if k in space}
        row.update(state='unknown', last_tidy=0, tidy_until=0)
        if jobs:
            rows = [board.get(job, {}) for job in jobs]
            if any(a.get('chore') in jobs for a in active.values()):
                row['state'] = 'doing'
            elif any(r.get('pending_by') for r in rows):
                row['state'] = 'waiting'
            elif any(a.get('chore') in jobs for a in paused.values()):
                row['state'] = 'paused'
            elif all(job in board for job in jobs):
                row['last_tidy'] = min(r.get('last_clean', 0) for r in rows)
                row['tidy_until'] = min(r.get('clean_until', 0) for r in rows)
                row['state'] = 'tidy' if row['tidy_until'] > now else 'untidy'
        else:
            latest = next((e for e in reversed(events) if e.get('type') == 'space_tidy' and e.get('space') == key), None)
            if latest:
                row['by'] = latest.get('by')
                row['last_tidy'] = latest['ts'] if latest.get('tidy') else 0
                row['tidy_until'] = latest['ts'] + float(space.get('cooldown_h', 72)) * 3600 if latest.get('tidy') else 0
                row['state'] = 'tidy' if row['tidy_until'] > now else 'untidy'
        # A resident can confirm the floor is picked up without completing a
        # shared chore or resolving a child's active job or pending inspection.
        if key in ready and row['state'] in ('unknown', 'untidy', 'tidy'):
            stamp = ready[key]
            until = stamp + float(space.get('cooldown_h', 72)) * 3600
            latest_change = max((changed.get(job, 0) for job in jobs), default=0)
            if not jobs and latest:
                latest_change = latest['ts']
            if stamp > max(row['last_tidy'], latest_change):
                until = max(until, row['tidy_until'])
                row.update(state='tidy' if now < until else 'untidy', last_tidy=stamp,
                           tidy_until=until, by='vacuum_request')
        result[key] = row
    return result
