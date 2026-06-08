import Link from 'next/link';
import { getDeployments, getDeploymentStats, getServices } from '@/lib/api';
import StatCard from '@/components/ui/StatCard';
import PageHeader from '@/components/ui/PageHeader';
import Panel from '@/components/ui/Panel';
import EmptyState from '@/components/ui/EmptyState';
import { RiskBadge, StatusPill, EnvBadge } from '@/components/ui/Badge';
import { Activity, ArrowRight, Rocket } from 'lucide-react';
import type { Deployment, DeploymentStats, Service } from '@/lib/types';

function fmt(iso: string) {
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) +
    ' ' + d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
}

export default async function OverviewPage() {
  let stats: DeploymentStats = { total_deployments: 0, stable: 0, failed: 0, success_rate: 0 };
  let deployments: Deployment[] = [];
  let services: Service[] = [];
  let error = false;

  try {
    [stats, deployments, services] = await Promise.all([
      getDeploymentStats(),
      getDeployments(1, 10).then(r => r.data),
      getServices(),
    ]);
  } catch {
    error = true;
  }

  const activeCount = deployments.filter(d =>
    ['DEPLOYING','DEPLOYED','MONITORING'].includes(d.status)
  ).length;

  return (
    <div className="flex flex-col gap-8 p-8 animate-fadein">
      {/* Header */}
      <PageHeader
        title="Platform Overview"
        subtitle="Deployments, risk signals, and service health across all environments."
      >
        <span className="flex items-center gap-2.5 rounded-full border border-emerald-500/15 bg-emerald-500/[0.04] px-4 py-2 text-xs font-medium text-emerald-400">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(52,211,153,0.4)]" />
          </span>
          All Systems Operational
        </span>
      </PageHeader>

      {error && (
        <div className="flex items-center gap-3 rounded-[12px] border border-yellow-500/15 bg-yellow-500/[0.04] px-5 py-3.5 text-sm text-yellow-300">
          <Activity className="h-4 w-4 flex-shrink-0" />
          Backend unavailable — start the FastAPI server on port 8000.
        </div>
      )}

      {/* Stat cards */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5 stagger-children">
        <StatCard label="Total Deployments" value={stats.total_deployments} sub="All time" />
        <StatCard label="Active Now" value={activeCount} color="blue" sub="Deploying / Monitoring" />
        <StatCard
          label="Success Rate"
          value={`${(stats.success_rate * 100).toFixed(1)}%`}
          color={stats.success_rate >= 0.9 ? 'green' : stats.success_rate >= 0.7 ? 'yellow' : 'red'}
          sub="Stable / Total"
        />
        <StatCard label="Failed" value={stats.failed} color={stats.failed > 0 ? 'red' : 'default'} sub="Needs attention" />
        <StatCard label="Services" value={services.length} sub="Registered" />
      </div>

      {/* Two column layout */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">

        {/* Recent deployments — 2 cols */}
        <div className="lg:col-span-2">
          <Panel
            title="Recent Deployments"
            actions={
              <Link href="/deployments" className="flex items-center gap-1.5 text-xs font-medium text-cyan-400 hover:text-cyan-300 transition-colors">
                View all
                <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            }
          >
            {deployments.length === 0 ? (
              <EmptyState
                icon={<Rocket className="h-5 w-5 text-cyan-400" />}
                title="No deployments yet"
                description="Register your first deployment via the API."
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-white/[0.05] bg-white/[0.015]">
                      {['ID', 'Env', 'Version', 'Status', 'Risk', 'When'].map(h => (
                        <th key={h} className="px-5 py-3 text-left text-[11px] font-semibold uppercase tracking-[0.07em] text-slate-500">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {deployments.map(d => (
                      <tr key={d.id} className="border-b border-white/[0.04] transition-colors hover:bg-cyan-500/[0.03]">
                        <td className="px-5 py-3">
                          <Link href={`/deployments/${d.id}`} className="font-mono text-xs text-cyan-400 hover:text-cyan-300 transition-colors">
                            {d.id.slice(0, 8)}…
                          </Link>
                        </td>
                        <td className="px-5 py-3"><EnvBadge env={d.environment} /></td>
                        <td className="px-5 py-3 font-mono text-xs text-slate-300">
                          {d.version ?? d.git_sha?.slice(0, 7) ?? '—'}
                        </td>
                        <td className="px-5 py-3"><StatusPill status={d.status} /></td>
                        <td className="px-5 py-3">
                          {d.risk_level
                            ? <RiskBadge level={d.risk_level} score={d.risk_score} />
                            : <span className="text-slate-600">—</span>}
                        </td>
                        <td className="px-5 py-5 font-mono text-xs text-slate-500">
                          {fmt(d.created_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>

        {/* Services sidebar — 1 col */}
        <div className="flex flex-col gap-3">
          <Panel
            title="Services"
            actions={
              <Link href="/services" className="flex items-center gap-1 text-xs font-medium text-cyan-400 hover:text-cyan-300 transition-colors">
                All
                <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            }
          >
            {services.length === 0 ? (
              <div className="py-10 text-center text-xs text-slate-500">No services</div>
            ) : (
              <div className="divide-y divide-white/[0.04]">
                {services.slice(0, 6).map(s => {
                  const score = s.stability_score ?? 100;
                  const color = score >= 80 ? '#10b981' : score >= 60 ? '#f59e0b' : '#ef4444';
                  return (
                    <div key={s.id} className="flex items-center gap-3 px-5 py-3.5 transition-colors hover:bg-cyan-500/[0.03]">
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-white">{s.name}</p>
                        <p className="text-[11px] text-slate-500">{s.environment ?? 'unknown'}</p>
                      </div>
                      <div className="text-right">
                        <p className="text-sm font-bold tabular-nums" style={{ color }}>{score}</p>
                        <div className="mt-1.5 h-1.5 w-16 overflow-hidden rounded-full bg-white/[0.06]">
                          <div
                            className="h-full rounded-full transition-all duration-700"
                            style={{ width: `${score}%`, background: color, boxShadow: `0 0 6px ${color}40` }}
                          />
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </Panel>
        </div>

      </div>
    </div>
  );
}