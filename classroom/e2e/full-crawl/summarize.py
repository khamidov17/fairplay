import collections
import json
import re
import sys

for fn in sys.argv[1:]:
    r = json.load(open(fn))
    for role, data in r.items():
        v = data['visited']
        print('==', role, 'pages', len(v), 'links', data.get('linksChecked'),
              dict(collections.Counter(x['status'] for x in v)))
        if data.get('external'):
            print('  external:', json.dumps(data['external'])[:600])
        c = collections.Counter((f['kind'], re.sub(r'\d+', 'N', f['url'].split('?')[0]),
                                 re.sub(r'\s+\[after clicking.*', '', f['detail'])[:200]) for f in data['findings'])
        for k, n in sorted(c.items()):
            print('  %3d %s' % (n, k))
        if '-v' in sys.argv[0:1] or True:
            bad = collections.Counter(x['status'] for x in v if x['status'] >= 400)
            for x in v:
                if x['status'] >= 500:
                    print('   status', x['status'], x['url'])
