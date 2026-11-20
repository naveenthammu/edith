import { Fan, Lightbulb } from 'lucide-react';
import { useState } from 'react';
import { errorText, post } from '../api';
import type { AppState, Device } from '../types';
import { EmptyState, ErrorNotice } from './Shared';
export function DeviceList({ state, refresh }: { state: AppState; refresh: () => Promise<void> }) {
  const [busy, setBusy] = useState(''); const [error, setError] = useState('');
  async function toggle(device: Device) { setBusy(device.id); setError(''); try { await post<Device>(`/devices/${encodeURIComponent(device.id)}/toggle`, { on: !device.on }); await refresh(); } catch (error) { setError(errorText(error)); } finally { setBusy(''); } }
  return <>{state.devices.length ? <div className="device-list">{state.devices.map(device => { const Icon = device.id.includes('fan') ? Fan : Lightbulb; return <div className="device-row" key={device.id}><Icon size={32} strokeWidth={1.5} /><div className="device-label"><span>{device.name}</span><small>{busy === device.id ? 'Updating…' : device.on ? 'On' : 'Off'}</small></div><button className={`switch ${device.on ? 'on' : ''}`} role="switch" aria-checked={device.on} aria-label={`${device.name} demo power`} disabled={Boolean(busy)} onClick={() => void toggle(device)}><span /></button></div>; })}</div> : <EmptyState title="No devices in this profile">Guest mode can explore household notes and play.</EmptyState>}{error && <ErrorNotice>{error}</ErrorNotice>}</>;
}
export function Home({ state, refresh }: { state: AppState; refresh: () => Promise<void> }) { return <section className="page-view"><div className="page-heading"><h2>A little help around home.</h2><p>Demonstration devices</p></div><p className="demo-notice">These switches update local demo state only. No physical devices are connected.</p><div className="home-devices"><DeviceList state={state} refresh={refresh} /></div><p className="quiet-note">{state.profile === 'child' ? 'Child mode can use the study light.' : 'Device access follows the selected demo profile.'}</p></section>; }
