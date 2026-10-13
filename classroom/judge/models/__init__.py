from reversion import revisions

from judge.models.ai_hints import AIHintLog
from judge.models.assignment_draft import AssignmentDraft, AssignmentDraftFile
from judge.models.choices import ACE_THEMES, EFFECTIVE_MATH_ENGINES, MATH_ENGINES_CHOICES, TIMEZONE
from judge.models.comment import Comment, CommentLock, CommentVote
from judge.models.contest import Contest, ContestMoss, ContestParticipation, ContestProblem, ContestSubmission, \
    ContestTag, Rating, contest_problem_display_name
from judge.models.editor_attachment import EditorAttachment
from judge.models.interface import BlogPost, MiscConfig, NavigationBar, validate_regex
from judge.models.notification import AdminNotification, InstructorVerificationRequest, TeacherVerificationRequest
from judge.models.problem import LanguageLimit, License, Problem, ProblemClarification, ProblemGroup, \
    ProblemPointsVote, ProblemSubmissionType, ProblemTemplate, ProblemTranslation, ProblemType, Solution, \
    SubmissionSourceAccess, TranslatedProblemQuerySet
from judge.models.problem_data import CHECKERS, ProblemData, ProblemHarness, ProblemTestCase, problem_data_storage, \
    problem_directory_file
from judge.models.profile import Class, ClassMaterial, Organization, OrganizationRequest, Profile, WebAuthnCredential
from judge.models.question import ProblemBlank, ProblemCodeConfig, ProblemFileConfig, \
    ProblemFillBlankConfig, ProblemImportDraft, ProblemImportImage, ProblemImportItem, \
    ProblemMCQConfig, ProblemMCQOption, ProblemShortAnswerConfig, SubmissionFile
from judge.models.runtime import Judge, Language, RuntimeVersion
from judge.models.seb import SEBConfigPreset
from judge.models.stored_file import StoredFileRecord
from judge.models.submission import SUBMISSION_RESULT, Submission, SubmissionManualGrade, SubmissionSource, \
    SubmissionTestCase
from judge.models.ticket import Ticket, TicketMessage

revisions.register(Profile, exclude=['points', 'last_access', 'ip', 'rating'])
revisions.register(Problem, follow=['language_limits'])
revisions.register(LanguageLimit)
revisions.register(Contest, follow=['contest_problems'])
revisions.register(ContestProblem)
revisions.register(Organization)
revisions.register(BlogPost)
revisions.register(Solution)
revisions.register(Judge, fields=['name', 'created', 'auth_key', 'description'])
revisions.register(Language)
revisions.register(Comment, fields=['author', 'time', 'page', 'score', 'body', 'hidden', 'parent'])
del revisions
