import type { ReactNode } from 'react'
import { label, tone } from '../format'

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow: string; title: string; description: string; actions?: ReactNode }) { return <div className="page-head"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{actions && <div className="page-actions">{actions}</div>}</div> }
export function Button({ children, variant = 'secondary', disabled, type = 'button', onClick }: { children: ReactNode; variant?: 'primary' | 'secondary' | 'good' | 'danger' | 'link'; disabled?: boolean; type?: 'button' | 'submit'; onClick?: () => void }) { return <button className={`button ${variant}`} disabled={disabled} type={type} onClick={onClick}>{children}</button> }
export function Card({ children, className = '', title, action }: { children: ReactNode; className?: string; title?: string; action?: ReactNode }) { return <section className={`card ${className}`}>{title ? <div className="card-header"><h2>{title}</h2>{action}</div> : null}{children}</section> }
export function Pill({ value, className = '' }: { value: unknown; className?: string }) { return <span className={`pill ${tone(value)} ${className}`}>{label(value)}</span> }
export function Metric({ name, value, note }: { name: string; value: ReactNode; note: string }) { return <div className="card metric-card"><div className="metric-label">{name}</div><div className="metric-value">{value}</div><div className="metric-note">{note}</div></div> }
export function Field({ label: fieldLabel, children, hint }: { label: string; children: ReactNode; hint?: string }) { return <label className="field"><span className="field-label">{fieldLabel}</span>{children}{hint && <span className="field-hint">{hint}</span>}</label> }
export function JsonBlock({ value }: { value: unknown }) { return <pre className="code-block">{JSON.stringify(value, null, 2)}</pre> }
