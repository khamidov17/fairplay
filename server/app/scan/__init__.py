"""Slice 6 D — paper exams: print booklets with QR codes, read scans, AI-drafted grades.

print.py     booklets (PDF) + the print layout (answer box rectangles in PDF points)
pipeline.py  scans -> pages -> QR -> page transform -> crops + MCQ bubble reads (sync, run in threads)
grading.py   AI draft per paper answer (never applied without a teacher)
models.py    PrintLayout, ExamRoster, ScanBatch, ScanPage
storage.py   where crops/previews/uploaded scans live on disk
"""
