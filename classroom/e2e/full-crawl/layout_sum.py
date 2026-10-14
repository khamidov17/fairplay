import json
import sys

r = json.load(open(sys.argv[1]))
for k, v in r.items():
    flags = []
    if v.get('status', 200) >= 400:
        flags.append('STATUS %s' % v['status'])
    if v.get('overflow', 0) > 2:
        flags.append('overflow %dpx: %s' % (v['overflow'], '; '.join(v.get('wide', [])[:4])))
    if v.get('overlaps'):
        flags.append('fixed overlap: ' + '; '.join(v['overlaps'][:4]))
    if v.get('fixedTall'):
        flags.append('tall fixed: ' + ', '.join(v['fixedTall']))
    print(k, v['url'], '->', v.get('final'), '|', ' || '.join(flags) if flags else 'ok')
