"""Local stand-in for the IDE mode of DKU's judge fork. Development only.

The site sends IDE runs and the workspace "Run test" as submissions to the hidden problem
``idepractice``, with the student's stdin in ``meta.custom_input`` (judge/bridge/judge_handler.py).
The stock ``dmoj`` judge from PyPI ignores that field, so this grader feeds it to the program
instead of the problem's test file, and accepts any run that finishes (the stock judge has no
``allac`` checker). The program's output comes back as the case output.
"""
from dmoj.graders.standard import StandardGrader
from dmoj.result import Result


class Grader(StandardGrader):
    def grade(self, case):
        try:
            stdin = self.problem.meta['custom_input'] or ''
        except (KeyError, TypeError):
            stdin = ''
        data = stdin.encode('utf-8') if isinstance(stdin, str) else bytes(stdin)

        result = Result(case)
        self._launch_process(case)
        error = self._interact_with_process(case, result, data)
        self.populate_result(error, result, self._current_proc)
        # No checker: a run that did not crash, time out or run out of memory is accepted.
        result.points = case.points if not result.result_flag else 0.0
        return result
