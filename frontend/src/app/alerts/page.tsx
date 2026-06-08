'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import StatCard from '@/components/ui/StatCard';
import PageHeader from '@/components/ui/PageHeader';
import Panel from '@/components/ui/Panel';
import EmptyState from '@/components/ui/EmptyState';
import { AlertTriangle, CheckCircle } from 'lucide-react';
import { useAuth } from '@/lib/auth';

interface AlertItem {
  id: string;
  severity: string;
  title: string;
  description: string;
  status: string;
  triggered_at: string;
}

export default function AlertsPage() {
  const searchParams = useSearchParams();
  const statusFilter = searchParams.get('status') ?? undefined;
  const { token, isAuthenticated, isLoading: authLoading, loginUrl } = useAuth();

  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [authRequired, setAuthRequired] = useState(false);

  useEffect(() => {
    if (authLoading) return;

    if (!isAuthenticated || !token) {
      setAuthRequired(true);
      setLoading(false);
      return;
    }

    async function fetchAlerts() {
      try {
        const url = `/api/v1/alerts?per_page=50${statusFilter ? `&status=${statusFilter}` : ''}`;
        const res = await fetch(url, {
          headers: { Authorization: `Bearer ${token}` },
          cache: 'no-store',
        });
        if (res.status === 401) {
          setAuthRequired(true);
        } else if (res.ok) {
          const data = await res.json();
          setAlerts(data.data ?? []);
        }
      } catch {
        // silently fail
      } finally {
        setLoading(false);
      }
    }

    fetchAlerts();
  }, [token, isAuthenticated, authLoading, statusFilter]);

  const open = alerts.filter(a => a.status === 'OPEN').length;
  const acked = alerts.filter(a => a.status === 'ACKNOWLEDGED').length;

  const STATUSES = ['OPEN', 'ACKNOWLEDGED', 'RESOLVED'];
  const sevColor = (s: string) =>
    s === 'CRITICAL' ? { bar: '#f87171', badge: 'border-red-500/15 bg-red-500/[0.06] text-red-400' } :
    s === 'HIGH'     ? { bar: '#f97316', badge: 'border-orange-500/15 bg-orange-500/[0.06] text-orange-400' } :
    s === 'WARNING'  ? { bar: '#fbbf24', badge: 'border-yellow-500/15 bg-yellow-500/[0.06] text-yellow-400' } :
                       { bar: '#60a5fa', badge: 'border-blue-500/15 bg-blue-500/[0.06] text-blue-400' };
  const statusBadge = (s: string) =>
    s === 'OPEN'         ? 'border-red-500/15 bg-red-500/[0.06] text-red-400' :
    s === 'ACKNOWLEDGED' ? 'border-yellow-500/15 bg-yellow-500/[0.06] text-yellow-400' :
                           'border-emerald-500/15 bg-emerald-500/[0.06] text-emerald-400';

  function fmt(iso: string) {
    return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' +
      new Date(iso).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
  }

  if (loading || authLoading) {
    return (
      <div className="flex flex-col gap-7 p-8 animate-fadein">
        <PageHeader title="Alerts" subtitle="Loading..." />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-7 p-8 animate-fadein">
      {/* Header */}
      <PageHeader
        title="Alerts"
        subtitle="Deployment incidents and risk events"
      >
        {open > 0 && (
          <span className="flex items-center gap-2 rounded-full border border-red-500/15 bg-red-500/[0.04] px-4 py-2 text-xs font-bold text-red-400 shadow-[0_0_16px_rgba(239,68,68,0.08)]">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-red-400 opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-red-500" />
            </span>
            {open} open
          </span>
        )}
      </PageHeader>

      {/* Stats */}
      <div className="grid grid-cols-3 gap-4 stagger-children">
        <StatCard label="Open"          value={open}            color={open > 0 ? 'red' : 'default'}    sub="Needs action" />
        <StatCard label="Acknowledged"  value={acked}           color={acked > 0 ? 'yellow' : 'default'} sub="In progress" />
        <StatCard label="Total Shown"   value={alerts.length}   sub="Last 50" />
      </div>

      {/* Auth notice or content */}
      {authRequired ? (
        <div className="rounded-[14px] border border-yellow-500/15 bg-yellow-500/[0.04] p-7">
          <h3 className="mb-2 font-semibold text-yellow-300">Authentication required</h3>
          <p className="mb-5 text-sm text-yellow-200/60">
            You need to sign in to view alerts.
          </p>
          <a
            href={loginUrl}
            className="inline-flex items-center gap-2 rounded-lg border border-cyan-500/20 bg-cyan-500/10 px-5 py-2.5 text-sm font-semibold text-cyan-400 transition hover:bg-cyan-500/20"
          >
            Sign in with GitHub →
          </a>
        </div>
      ) : (
        <>
          {/* Filter bar */}
          <div className="flex flex-wrap gap-2">
            <Link href="/alerts" className={`ds-filter-chip ${!statusFilter ? 'active' : ''}`}>All</Link>
            {STATUSES.map(s => (
              <Link key={s} href={`/alerts?status=${s}`} className={`ds-filter-chip ${statusFilter === s ? 'active' : ''}`}>
                {s}
              </Link>
            ))}
          </div>

          {/* Alert list */}
          <Panel
            title={`${alerts.length} Alert${alerts.length !== 1 ? 's' : ''}${statusFilter ? ` — ${statusFilter}` : ''}`}
          >
            {alerts.length === 0 ? (
              <EmptyState
                icon={<CheckCircle className="h-5 w-5 text-emerald-400" />}
                title={`No alerts${statusFilter ? ` with status ${statusFilter}` : ''}`}
                description="Alerts are created when deployment risk is HIGH or CRITICAL."
              />
            ) : (
              <div className="divide-y divide-white/[0.04]">
                {alerts.map(a => {
                  const { bar, badge } = sevColor(a.severity ?? 'INFO');
                  return (
                    <div key={a.id} className="flex items-start gap-3.5 px-5 py-4 hover:bg-cyan-500/[0.03] transition-colors">
                      {/* Severity bar with glow */}
                      <div
                        className="mt-1.5 h-12 w-[3px] flex-shrink-0 rounded-full"
                        style={{ background: bar, boxShadow: `0 0 8px ${bar}40` }}
                      />
                      <div className="flex-1 min-w-0">
                        <p className="text-[13.5px] font-semibold text-white">{a.title ?? 'Alert'}</p>
                        {a.description && (
                          <p className="mt-0.5 text-xs text-slate-400 line-clamp-2">{a.description}</p>
                        )}
                        <div className="mt-2.5 flex flex-wrap gap-2">
                          <span className={`rounded-full border px-2.5 py-0.5 text-[11px] font-bold ${badge}`}>
                            {a.severity ?? 'INFO'}
                          </span>
                          <span className={`rounded-full border px-2.5 py-0.5 text-[11px] font-bold ${statusBadge(a.status)}`}>
                            {a.status}
                          </span>
                        </div>
                      </div>
                      <p className="flex-shrink-0 font-mono text-[11px] text-slate-600">
                        {fmt(a.triggered_at)}
                      </p>
                    </div>
                  );
                })}
              </div>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}
