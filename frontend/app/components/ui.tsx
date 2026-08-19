import type { ReactNode } from "react";

type Tone = "neutral" | "positive" | "negative" | "warning" | "info";

export function Card({
  children,
  className = "",
  as: Component = "section"
}: {
  children: ReactNode;
  className?: string;
  as?: "section" | "article" | "div";
}) {
  return <Component className={`card ${className}`}>{children}</Component>;
}

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function Button({
  children,
  variant = "primary",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
}) {
  return (
    <button className={`button button-${variant} ${className}`} {...props}>
      {children}
    </button>
  );
}

export function Field({
  label,
  children,
  hint
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint ? <small>{hint}</small> : null}
    </label>
  );
}

export function MetricCard({
  label,
  value,
  meta,
  tone = "neutral"
}: {
  label: string;
  value: string;
  meta?: string;
  tone?: Tone;
}) {
  return (
    <Card className="metric-card" as="article">
      <span className="metric-label">{label}</span>
      <strong className={`metric-value metric-${tone}`}>{value}</strong>
      {meta ? <span className="metric-meta">{meta}</span> : null}
    </Card>
  );
}

export function Skeleton({ rows = 1 }: { rows?: number }) {
  return (
    <div className="skeleton-stack" aria-label="Loading">
      {Array.from({ length: rows }, (_, index) => (
        <div className="skeleton-line" key={index} />
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  message,
  action
}: {
  title: string;
  message: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <h3>{title}</h3>
      <p>{message}</p>
      {action}
    </div>
  );
}

export function ErrorState({
  message,
  correlationId,
  action
}: {
  message: string;
  correlationId?: string;
  action?: ReactNode;
}) {
  return (
    <div className="error-state" role="alert">
      <h3>Unable to load data</h3>
      <p>{message}</p>
      {correlationId ? <small>Correlation ID: {correlationId}</small> : null}
      {action}
    </div>
  );
}
