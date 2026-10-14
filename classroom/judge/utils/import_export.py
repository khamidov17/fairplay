
import csv
import io
import re
import zipfile
from typing import Dict, Optional, Tuple

from django.contrib.auth.models import User
from django.db import transaction
from django.db.utils import IntegrityError
from django.utils.translation import gettext as _

from judge.models import (
    Class, Contest, ContestParticipation, ContestSubmission, Organization, Profile, SubmissionSource)

# Canonical import fields -> accepted CSV headers (case-insensitive).
# Canvas roster exports use NetID / First name / Last Name / Email (and often
# netid@duke.edu as a second email column); prefer Email when both are present.
# Accounts are NetID/SSO logins: no local password is set on create.
CSV_FIELD_ALIASES = {
    'username': ('username', 'netid', 'sis login id', 'login id'),
    'first_name': ('first_name', 'first name', 'firstname'),
    'last_name': ('last_name', 'last name', 'lastname', 'surname'),
    'student': ('student',),
    'email': ('email', 'e-mail', 'netid@duke.edu'),
}
SCORE_FIELD_ALIASES = {
    'score': (
        'score',
        'final grade',
        'grade',
        'points',
        'total score',
        'current grade',
        'current score',
        'final score',
    ),
}
REQUIRED_CSV_FIELDS = ('username', 'first_name', 'last_name', 'email')
_SCORE_VALUE_RE = re.compile(r'^(-?\d+(?:\.\d+)?)(?:\s*%|\s*/\s*\d+(?:\.\d+)?)?$')


class UserImportResult:
    def __init__(self):
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.errors = []
        self.org_linked = 0
        self.class_linked = 0


def _header_key(name: str) -> str:
    return ' '.join((name or '').strip().lower().split())


def resolve_csv_headers(fieldnames) -> Dict[str, str]:
    """Map each canonical field to the first matching original header name."""
    by_key = {}
    for header in fieldnames or []:
        if header is None:
            continue
        key = _header_key(header)
        if key and key not in by_key:
            # Keep the exact DictReader key so row lookups succeed.
            by_key[key] = header

    resolved = {}
    for canon, aliases in CSV_FIELD_ALIASES.items():
        for alias in aliases:
            original = by_key.get(_header_key(alias))
            if original is not None:
                resolved[canon] = original
                break
    return resolved


def resolve_csv_score_headers(fieldnames) -> list[str]:
    """Return every plausible score column, preserving CSV column order."""
    candidates = []
    for header in fieldnames or []:
        if header is None:
            continue
        key = _header_key(header)
        if not key:
            continue
        if key in SCORE_FIELD_ALIASES['score'] or 'grade' in key or 'score' in key:
            candidates.append(header)
    return candidates


def resolve_csv_score_header(fieldnames) -> Optional[str]:
    """Return the score column only when the CSV has one unambiguous candidate."""
    candidates = resolve_csv_score_headers(fieldnames)
    return candidates[0] if len(candidates) == 1 else None


def csv_row_to_user_fields(raw_row: dict, header_map: Dict[str, str]) -> dict:
    """Pull username / names / email from a raw CSV row via aliases."""
    def get(canon: str) -> str:
        src = header_map.get(canon)
        if not src:
            return ''
        return (raw_row.get(src) or '').strip()

    email = get('email')
    if not email:
        # Canvas rosters have both "Email" and "netid@duke.edu"; a student with a blank
        # Email cell still gets the NetID address from the other column.
        by_key = {_header_key(k): k for k in raw_row if k is not None}
        for alias in CSV_FIELD_ALIASES['email']:
            src = by_key.get(_header_key(alias))
            if src and (raw_row.get(src) or '').strip():
                email = raw_row[src].strip()
                break

    return {
        'username': get('username'),
        'first_name': get('first_name'),
        'last_name': get('last_name'),
        'student': get('student'),
        'email': email,
    }


def _normalize_participant_name(value: str) -> str:
    """Normalize names for stable, case-insensitive matching."""
    return ' '.join((value or '').replace(',', ' ').lower().split())


def _build_name_key(first_name: str, last_name: str) -> str:
    first_normalized = _normalize_participant_name(first_name)
    last_normalized = _normalize_participant_name(last_name)
    if not first_normalized or not last_normalized:
        return ''
    return ' '.join(part for part in (first_normalized, last_normalized) if part)


def _student_name_keys(student_name: str) -> set[str]:
    """Return possible name keys for a Canvas Student value."""
    normalized = _normalize_participant_name(student_name)
    if not normalized:
        return set()

    keys = {normalized}
    if ',' in student_name:
        first_part, last_part = (part.strip() for part in student_name.split(',', 1))
        keys.update({
            _build_name_key(first_part, last_part),
            _build_name_key(last_part, first_part),
        })
    return keys - {''}


def _find_participation_by_name(user_fields: dict, by_name: dict):
    """Find a unique participant using split-name or Canvas Student fields."""
    name_keys = set()
    if user_fields['first_name'] and user_fields['last_name']:
        name_keys.add(_build_name_key(user_fields['first_name'], user_fields['last_name']))
    name_keys.update(_student_name_keys(user_fields['student']))

    matches = {
        participation.pk: participation
        for name_key in name_keys
        for participation in by_name.get(name_key, [])
    }
    return next(iter(matches.values())) if len(matches) == 1 else None


class ContestScoreImportResult:
    def __init__(self):
        self.updated = 0
        self.skipped = 0
        self.errors = []
        self.applied = False


class ContestScoreJoinResult:
    def __init__(self):
        self.csv_content = ''
        self.errors = []
        self.joined = 0


def export_contest_scores_joined_csv(
        contest: Contest, csv_file, score_column: Optional[str] = None) -> ContestScoreJoinResult:
    """Write DKUOJ scores to a selected Canvas column without adding OJ-only participants."""
    result = ContestScoreJoinResult()

    try:
        decoded_file = csv_file.read().decode('utf-8-sig').splitlines()
    except UnicodeDecodeError:
        result.errors.append(_('File is not a valid UTF-8 CSV.'))
        return result

    reader = csv.DictReader(decoded_file)
    if not reader.fieldnames:
        result.errors.append(_('CSV file is empty or missing headers.'))
        return result

    fieldnames = list(reader.fieldnames)
    header_map = resolve_csv_headers(fieldnames)
    can_match_by_name = (
        'student' in header_map or
        ('first_name' in header_map and 'last_name' in header_map)
    )
    if 'username' not in header_map and not can_match_by_name:
        result.errors.append(_('Missing identifier columns. Provide username/NetID or both first and last name.'))
        return result

    participations = ContestParticipation.objects.filter(
        contest=contest,
        virtual=ContestParticipation.LIVE,
        is_disqualified=False,
    ).select_related('user__user')
    by_username = {
        (participation.user.user.username or '').casefold(): participation
        for participation in participations
    }
    by_name = {}
    for participation in participations:
        user = participation.user.user
        name_key = _build_name_key(user.first_name, user.last_name)
        if name_key:
            by_name.setdefault(name_key, []).append(participation)

    requested_score_header = (score_column or '').strip() or contest.name or 'DKUOJ Score'
    matching_header = next(
        (header for header in fieldnames if _header_key(header) == _header_key(requested_score_header)),
        None,
    )
    score_header = matching_header or requested_score_header
    output_fieldnames = fieldnames if matching_header else fieldnames + [score_header]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=output_fieldnames, extrasaction='ignore')
    writer.writeheader()
    for raw_row in reader:
        row = {fieldname: raw_row.get(fieldname, '') for fieldname in fieldnames}
        user_fields = csv_row_to_user_fields(raw_row, header_map)
        participation = None
        username = user_fields['username']
        if username:
            participation = by_username.get(username.casefold())
        if participation is None:
            participation = _find_participation_by_name(user_fields, by_name)

        row[score_header] = ''
        if participation is not None:
            row[score_header] = round(float(participation.score), contest.points_precision)
            result.joined += 1
        writer.writerow(row)

    # Excel opens UTF-8 CSV files correctly when the BOM is present.
    result.csv_content = '\ufeff{}'.format(output.getvalue())
    return result


def _parse_score_value(raw_value) -> Optional[float]:
    """Parse a loose score format from Canvas or contest-grade CSV rows."""
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    if not value:
        return None
    match = _SCORE_VALUE_RE.fullmatch(value.replace(',', ''))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def import_contest_scores_from_csv(contest: Contest, csv_file,
                                   score_column: Optional[str] = None) -> ContestScoreImportResult:
    """Apply scores from a Canvas/exported CSV to live, non-disqualified participants."""
    result = ContestScoreImportResult()

    try:
        decoded_file = csv_file.read().decode('utf-8-sig').splitlines()
    except UnicodeDecodeError:
        result.errors.append(_('File is not a valid UTF-8 CSV.'))
        return result

    reader = csv.DictReader(decoded_file)
    if not reader.fieldnames:
        result.errors.append(_('CSV file is empty or missing headers.'))
        return result

    header_map = resolve_csv_headers(reader.fieldnames)
    score_candidates = resolve_csv_score_headers(reader.fieldnames)
    can_match_by_name = 'first_name' in header_map and 'last_name' in header_map
    if 'username' not in header_map and not can_match_by_name:
        result.errors.append(_('Missing identifier columns. Provide username/NetID or both first and last name.'))
        return result
    score_columns_by_key = {_header_key(column): column for column in score_candidates}
    if score_column:
        score_field = score_columns_by_key.get(_header_key(score_column))
        if score_field is None:
            result.errors.append(_('The selected score column does not exist in this CSV.'))
            return result
    elif len(score_candidates) == 1:
        score_field = score_candidates[0]
    elif not score_candidates:
        result.errors.append(_('Missing score column.'))
        return result
    else:
        result.errors.append(
            _('Multiple score columns were found (%(columns)s). Choose one before importing.') % {
                'columns': ', '.join(score_candidates),
            })
        return result

    participations = ContestParticipation.objects.filter(
        contest=contest,
        virtual=ContestParticipation.LIVE,
        is_disqualified=False,
    ).select_related('user__user')
    participations = participations.order_by('id')
    by_username = {
        (p.user.user.username or '').lower(): p
        for p in participations
    }
    by_name = {}
    for participation in participations:
        first_name = participation.user.user.first_name or ''
        last_name = participation.user.user.last_name or ''
        key = _build_name_key(first_name, last_name)
        if not key:
            continue
        by_name.setdefault(key, []).append(participation)

    seen_source_identifiers = set()
    seen_participation_ids = set()
    updates = []
    for row_idx, raw in enumerate(reader, start=2):
        row = csv_row_to_user_fields(raw, header_map)
        username = row['username']
        first_name = row['first_name']
        last_name = row['last_name']
        name_key = _build_name_key(first_name, last_name)
        username_lower = username.lower()

        if username:
            seen_key = f'u:{username_lower}'
            row_label = username
        elif name_key:
            seen_key = f'n:{name_key}'
            row_label = f'{first_name} {last_name}'
        else:
            result.skipped += 1
            result.errors.append(
                _('Row %(row)d: row is missing identifier information.') % {
                    'row': row_idx,
                })
            continue

        if seen_key in seen_source_identifiers:
            result.skipped += 1
            result.errors.append(
                _('Row %(row)d: duplicate participant (%(participant)s), row ignored.') % {
                    'row': row_idx,
                    'participant': row_label,
                })
            continue
        seen_source_identifiers.add(seen_key)

        score_value = _parse_score_value(raw.get(score_field))
        if score_value is None:
            result.skipped += 1
            result.errors.append(
                _('Row %(row)d: unable to parse score for %(identifier)s.') % {
                    'row': row_idx,
                    'identifier': row_label,
                })
            continue

        participation = None
        if username:
            participation = by_username.get(username_lower)
        elif name_key:
            matches = by_name.get(name_key, [])
            if len(matches) > 1:
                result.skipped += 1
                result.errors.append(
                    _(
                        'Row %(row)d: "%(participant)s" matches more than one participant; '
                        'please resolve in import file.',
                    ) % {
                        'row': row_idx,
                        'participant': row_label,
                    })
                continue
            if matches:
                participation = matches[0]

        if participation is None:
            result.skipped += 1
            result.errors.append(
                _('Row %(row)d: %(identifier)s is not currently a participant of this assignment.') % {
                    'row': row_idx,
                    'identifier': row_label,
                })
            continue

        if participation.id in seen_participation_ids:
            result.skipped += 1
            result.errors.append(
                _('Row %(row)d: %(identifier)s duplicates a participant from an earlier row.') % {
                    'row': row_idx,
                    'identifier': row_label,
                })
            continue
        seen_participation_ids.add(participation.id)
        updates.append((participation.id, max(0, score_value)))

    if result.errors:
        result.errors.insert(0, _('No scores were updated because the CSV has errors.'))
        return result

    with transaction.atomic():
        locked_participations = ContestParticipation.objects.select_for_update().filter(
            id__in=[participation_id for participation_id, _ in updates],
            contest=contest,
            virtual=ContestParticipation.LIVE,
            is_disqualified=False,
        )
        locked_by_id = {participation.id: participation for participation in locked_participations}
        if len(locked_by_id) != len(updates):
            result.errors.append(_('Participants changed while importing; no scores were updated.'))
            return result
        for participation_id, score_value in updates:
            participation = locked_by_id[participation_id]
            participation.score = round(float(score_value), contest.points_precision)
            participation.save(update_fields=['score'])
            result.updated += 1

    result.applied = True

    return result


def process_user_csv(csv_file, organization: Organization = None, target_class: Class = None,
                     update_existing: bool = False, activate: bool = True) -> UserImportResult:
    """
    Parses a CSV file object and imports users for course/class membership.

    Accepts legacy columns (username, first_name, last_name, email) and Canvas
    roster exports (NetID, First name, Last Name, Email, …).

    New accounts use username=NetID with an unusable local password; students
    sign in via Duke NetID SSO. Existing accounts are linked to the course/class.

    ``update_existing`` only ever touches the name/e-mail/active state of an
    existing account that is a plain member (not staff, not superuser, not a
    course/class manager) already enrolled in the target class (or course,
    when no class is given). Any other existing account is only linked: a
    roster upload must never be able to rewrite another person's e-mail
    address (account takeover through password reset) or re-activate a
    disabled account. Even for members, only SSO-only accounts are changed
    (``import_may_change_login_fields``), because an earlier upload of the
    same file makes any account a member.
    """
    result = UserImportResult()

    # Read CSV content
    try:
        decoded_file = csv_file.read().decode('utf-8-sig').splitlines()
    except UnicodeDecodeError:
        result.errors.append(_('File is not a valid UTF-8 CSV.'))
        return result

    reader = csv.DictReader(decoded_file)
    if not reader.fieldnames:
        result.errors.append(_('CSV file is empty or missing headers.'))
        return result

    header_map = resolve_csv_headers(reader.fieldnames)
    missing = [field for field in REQUIRED_CSV_FIELDS if field not in header_map]
    if missing:
        result.errors.append(_('Missing required columns: %s') % ', '.join(sorted(missing)))
        return result

    seen_usernames = set()
    for row_idx, raw in enumerate(reader, start=1):
        row = csv_row_to_user_fields(raw, header_map)
        username = row.get('username')
        if not username:
            continue

        if username in seen_usernames:
            result.skipped += 1
            result.errors.append(_('Row %(row)d (%(username)s): duplicate username within this CSV; '
                                   'row ignored.') % {'row': row_idx, 'username': username})
            continue
        seen_usernames.add(username)

        try:
            with transaction.atomic():
                outcome, user = _upsert_user(row, update_existing, activate,
                                             organization=organization, target_class=target_class)
                if outcome == 'created':
                    result.created += 1
                elif outcome == 'updated':
                    result.updated += 1
                else:
                    result.skipped += 1

                if organization and user:
                    if _add_user_to_org(user, organization):
                        result.org_linked += 1

                if target_class and user:
                    if _add_user_to_class(user, target_class):
                        result.class_linked += 1

        except Exception as exc:
            result.errors.append(_('Row %(row)d (%(username)s): %(error)s') % {
                'row': row_idx, 'username': username, 'error': str(exc),
            })

    return result


def _ensure_profile(user: User) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def import_may_update_existing_user(user: User, organization: Organization = None,
                                    target_class: Class = None) -> bool:
    """May a roster import change this existing account's name/e-mail/active flag?

    Only for a plain, non-staff account that is already a member of the target
    class (or of the course when no class is given) and manages nothing in it.
    """
    if user.is_staff or user.is_superuser:
        return False
    if target_class is None and organization is None:
        return False
    profile = Profile.objects.filter(user=user).first()
    if profile is None:
        return False
    if target_class is not None:
        if not target_class.members.filter(id=profile.id).exists():
            return False
        if target_class.admins.filter(id=profile.id).exists():
            return False
        organization = organization or target_class.organization
    if organization is not None:
        if not organization.members.filter(id=profile.id).exists():
            return False
        if organization.admins.filter(id=profile.id).exists():
            return False
        if organization.instructors.filter(id=profile.id).exists():
            return False
        if Class.objects.filter(organization=organization, admins=profile).exists():
            return False
    return True


def import_may_change_login_fields(user: User) -> bool:
    """May a roster import change this account's e-mail address or re-activate it?

    Only for an SSO-only account: no usable local password (so a password reset cannot reach
    it) and no social-auth login (``judge.social_auth.associate_by_email`` never links one either).
    Names follow the same rule.
    """
    if user.has_usable_password():
        return False
    from social_django.models import UserSocialAuth
    return not UserSocialAuth.objects.filter(user=user).exists()


def _upsert_user(row: dict, update_existing: bool, activate: bool,
                 organization: Organization = None, target_class: Class = None) -> Tuple[str, Optional[User]]:
    username = row.get('username')
    first_name = row.get('first_name', '')
    last_name = row.get('last_name', '')
    email = row.get('email', '')

    try:
        user = User.objects.get(username=username)
        if update_existing and import_may_update_existing_user(user, organization, target_class):
            # Membership is not enough: the upload itself links every row's account to the
            # course, so a second upload of the same file would pass the check above for any
            # account (CA-1). So the import only edits SSO-only accounts nobody can sign
            # into with a password or a social login: e-mail and the active flag are what a
            # takeover needs (password reset, re-enabling), and real names show in search.
            sso_only = import_may_change_login_fields(user)
            updated_fields = []
            if first_name and user.first_name != first_name and sso_only:
                user.first_name = first_name
                updated_fields.append('first_name')
            if last_name and user.last_name != last_name and sso_only:
                user.last_name = last_name
                updated_fields.append('last_name')
            if email and user.email != email and sso_only:
                user.email = email
                updated_fields.append('email')
            if activate and not user.is_active and sso_only:
                user.is_active = True
                updated_fields.append('is_active')

            _ensure_profile(user)
            if updated_fields:
                user.save(update_fields=updated_fields)
                return 'updated', user
            # Nothing changed (protected account or same data): report it as skipped,
            # so "Updated: N" on the result page means N accounts were really changed.
            return 'skipped', user
        else:
            return 'skipped', user
    except User.DoesNotExist:
        try:
            # Pre-register for NetID SSO (same as CustomNetidLoginView): username is
            # the NetID, local password is unusable — students sign in with SSO only.
            user = User.objects.create_user(
                username=username,
                email=email or '',
            )
            user.set_unusable_password()
            user.first_name = first_name or ''
            user.last_name = last_name or ''
            if activate:
                user.is_active = True
            user.save()
            _ensure_profile(user)
            return 'created', user
        except IntegrityError as e:
            raise Exception(f'Integrity Error: {str(e)}')


def _add_user_to_org(user: User, org: Organization) -> bool:
    profile = _ensure_profile(user)
    if org.members.filter(id=profile.id).exists():
        return False
    org.members.add(profile)
    return True


def _add_user_to_class(user: User, target_class: Class) -> bool:
    profile = _ensure_profile(user)
    if target_class.members.filter(id=profile.id).exists():
        return False
    target_class.members.add(profile)
    return True


# --- Contest Export Logic ---

def export_contest_scores_csv(contest: Contest, include_disqualified: bool = False,
                              include_virtual: bool = False) -> str:
    """
    Generates a CSV string of contest scores.
    """
    qs = ContestParticipation.objects.filter(contest=contest)
    if not include_disqualified:
        qs = qs.filter(is_disqualified=False)
    if not include_virtual:
        qs = qs.filter(virtual=ContestParticipation.LIVE)

    qs = qs.select_related('user__user').order_by('-score', 'cumtime', 'user__user__username')

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Student', 'SIS Login ID', contest.name or 'DKUOJ Score'])

    for participation in qs:
        user = participation.user.user
        writer.writerow([
            ', '.join(part for part in (user.last_name, user.first_name) if part),
            user.username or '',
            participation.score,
        ])

    # Excel opens UTF-8 CSV files correctly when the BOM is present.
    return '\ufeff{}'.format(output.getvalue())


def export_contest_submissions_zip(contest: Contest, include_disqualified: bool = False,
                                   include_virtual: bool = False, latest_only: bool = True) -> bytes:
    """
    Generates a ZIP file (bytes) of contest submissions.
    """
    qs = ContestSubmission.objects.filter(participation__contest=contest)

    if not include_disqualified:
        qs = qs.filter(participation__is_disqualified=False)
    if not include_virtual:
        qs = qs.filter(participation__virtual=ContestParticipation.LIVE)

    qs = qs.select_related(
        'participation__user__user',
        'submission__user__user',
        'submission__problem',
        'submission__language',
        'problem__problem',
    ).prefetch_related('submission__source')

    if latest_only:
        qs = qs.order_by('submission__user_id', 'submission__problem_id', '-submission__date', '-submission__id')
    else:
        qs = qs.order_by('submission__user__user__username', 'submission__problem__code', 'submission__id')

    zip_buffer = io.BytesIO()
    seen_pairs = set()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for cs in qs.iterator():
            sub = cs.submission

            if latest_only:
                key = (sub.user_id, sub.problem_id)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)

            try:
                source_obj = sub.source
            except SubmissionSource.DoesNotExist:
                continue

            if not source_obj or not source_obj.source:
                continue

            prof_user = sub.user.user
            username = prof_user.username or f'user{prof_user.id}'
            problem_code = (sub.problem.code or cs.problem.problem.code or f'prob{cs.problem_id}')

            ext = sub.language.extension or 'txt'
            fname = f'{username}/{problem_code}-{sub.id}.{ext}'
            zf.writestr(fname, source_obj.source)

    return zip_buffer.getvalue()
