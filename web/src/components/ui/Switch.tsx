// Accessible on/off switch (role="switch"), styled like the design toggles (44x26).
// Styles are inline so this works on any page without extra CSS.

import type { CSSProperties } from "react";

interface Props {
  checked: boolean;
  onChange: (checked: boolean) => void;
  /** Accessible name when there is no visible <label htmlFor>. */
  label?: string;
  id?: string;
  disabled?: boolean;
  describedBy?: string;
}

export function Switch({ checked, onChange, label, id, disabled, describedBy }: Props) {
  const track: CSSProperties = {
    width: 44,
    height: 26,
    minWidth: 44,
    borderRadius: 99,
    border: "none",
    padding: 0,
    position: "relative",
    background: checked ? "var(--primary)" : "var(--border-strong, #D5D9E3)",
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.6 : 1,
    transition: "background 120ms ease",
    flexShrink: 0,
  };
  const knob: CSSProperties = {
    position: "absolute",
    top: 3,
    left: checked ? 21 : 3,
    width: 20,
    height: 20,
    borderRadius: "50%",
    background: "#fff",
    boxShadow: "0 1px 2px rgba(20,22,31,0.2)",
    transition: "left 120ms ease",
  };
  return (
    <button
      type="button"
      role="switch"
      id={id}
      aria-checked={checked}
      aria-label={label}
      aria-describedby={describedBy}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      style={track}
    >
      <span style={knob} aria-hidden="true" />
    </button>
  );
}
