import re
s = open('server.py',encoding='utf-8').read()
m = re.search(r'MAIN_HTML\s*=\s*r?"""(.*)"""', s, re.S)
if not m:
    print('MAIN_HTML not found')
else:
    html = m.group(1)
    lines = html.splitlines()
    print('MAIN_HTML lines:', len(lines))
    n=859
    if n<=len(lines):
        print('line',n,':')
        print(lines[n-1])
        for i in range(n-40,n+5):
            if 1<=i<=len(lines):
                print(f'{i:4}: {lines[i-1]}')
    else:
        print('MAIN_HTML shorter than',n)
