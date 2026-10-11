// Client-side checks for the Create account form. They mirror the server rules in
// server/app/routes_register.py; the server stays the authority.
import type { Role } from "../api/types";

export interface RegisterForm {
  name: string;
  email: string;
  username: string;
  password: string;
  confirm: string;
  role: Role | "";
}

export type RegisterErrors = Partial<Record<keyof RegisterForm, string>>;

export const MIN_PASSWORD = 8;
const USERNAME_RE = /^[a-z0-9][a-z0-9._-]{2,31}$/;
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export function passwordProblem(password: string, username = "", email = ""): string | null {
  if (password.length < MIN_PASSWORD) return `Use at least ${MIN_PASSWORD} characters.`;
  if (!/[A-Za-z]/.test(password) || !/[^A-Za-z]/.test(password))
    return "Use letters and at least one digit or symbol.";
  const low = password.toLowerCase();
  const local = email.split("@")[0] ?? "";
  if ((username && low.includes(username.toLowerCase())) || (local.length >= 3 && low.includes(local.toLowerCase())))
    return "Do not put your username or email in the password.";
  if (new Set(password).size < 4) return "This password is too simple.";
  return null;
}

export function validateRegister(f: RegisterForm): RegisterErrors {
  const errors: RegisterErrors = {};
  const name = f.name.trim().replace(/\s+/g, " ");
  const email = f.email.trim().toLowerCase();
  const username = f.username.trim().toLowerCase();
  if (name.length < 2) errors.name = "Enter your full name.";
  else if (name.length > 100) errors.name = "Name must be at most 100 characters.";
  if (!EMAIL_RE.test(email)) errors.email = "Enter a valid email address.";
  if (!USERNAME_RE.test(username))
    errors.username = "3–32 characters: letters, digits, dot, dash or underscore.";
  const pw = passwordProblem(f.password, username, email);
  if (pw) errors.password = pw;
  if (!f.confirm) errors.confirm = "Type the password again.";
  else if (f.confirm !== f.password) errors.confirm = "Passwords do not match.";
  if (f.role !== "teacher" && f.role !== "student") errors.role = "Choose Teacher or Student.";
  return errors;
}
