from server import app

with app.test_client() as c:
    # 模拟登录会话
    with c.session_transaction() as sess:
        sess['logged_in'] = True
        sess['user'] = 'test'

    r = c.get('/api/status')
    print('/api/status', r.status_code)
    try:
        print(r.get_json())
    except Exception as e:
        print('status json error', e)

    r2 = c.get('/api/schedule')
    print('/api/schedule', r2.status_code)
    try:
        j = r2.get_json()
        print('schedule keys:', list(j.keys()))
        # show courses count
        print('dedup total_course_count:', j.get('data',{}).get('total_course_count'))
        print('flat length:', len(j.get('flat') or []))
    except Exception as e:
        print('schedule json error', e)
