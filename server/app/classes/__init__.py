"""Classes (Google-Classroom style): a teacher's class with a join code, its members,
and exam <-> class assignment. API in app.routes_classes; eligibility rule in
app.classes.service (see its module docstring).

models.py    SchoolClass ("classes"), ClassMember ("class_members"), ExamClass ("exam_classes")
service.py   join codes, student lookup (username / email), CSV parsing, exam_gate
"""
