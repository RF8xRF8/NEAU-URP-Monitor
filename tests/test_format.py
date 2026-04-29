from monitor import _fmt_schedule_changes, _fmt_score_changes, _fmt_gpa_changes

# Test with minimal changes
schedule = [{'action': 'added', 'course': {'kcm': 'Test Course'}}]
scores = [{'action': 'added', 'score': {'kcm': 'Test Score', 'cj': '95'}}]
gpa = [{'fields': {'gpa': {'before': '3.0', 'after': '3.1'}}}]

s1 = _fmt_schedule_changes(schedule)
s2 = _fmt_score_changes(scores, '成绩')
s3 = _fmt_gpa_changes(gpa)

print('Summary examples for display and notification:')
print('Schedule:', s1)
print('Scores:', s2)
print('GPA:', s3)
