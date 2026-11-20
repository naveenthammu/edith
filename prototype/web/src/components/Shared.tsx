import { AlertCircle, BookOpen, Check, LoaderCircle } from 'lucide-react';
import type { Citation } from '../types';
export function ErrorNotice({ children }: { children: React.ReactNode }) { return <div className="error-notice" role="alert"><AlertCircle size={18} /><span>{children}</span></div>; }
export function Busy({ label = 'Working locally…' }: { label?: string }) { return <span className="busy" role="status"><LoaderCircle size={17} className="spin" />{label}</span>; }
export function Success({ children }: { children: React.ReactNode }) { return <div className="success-notice" role="status"><Check size={18} />{children}</div>; }
export function Citations({ citations }: { citations: Citation[] }) { return citations.length ? <div className="citations"><h4><BookOpen size={15} /> Sources · local text search</h4>{citations.map((citation, index) => <details key={`${citation.document_id}-${index}`}><summary>{citation.title}</summary><p>{citation.excerpt}</p></details>)}</div> : null; }
export function EmptyState({ title, children }: { title: string; children: React.ReactNode }) { return <div className="empty-state"><h3>{title}</h3><p>{children}</p></div>; }
export function timeLabel(value: string) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? value : date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
