'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { LayoutDashboard, Rocket, Server, Shield, AlertTriangle, LogOut } from 'lucide-react';

/** GitHub Octocat SVG — lucide-react doesn't include a GitHub icon in this version */
function GithubIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z" />
    </svg>
  );
}
import { useAuth } from '@/lib/auth';

const navItems = [
  { href: '/',            label: 'Overview',      icon: LayoutDashboard },
  { href: '/deployments', label: 'Deployments',   icon: Rocket },
  { href: '/services',    label: 'Services',      icon: Server },
  { href: '/risk',        label: 'Risk Analysis',  icon: Shield },
  { href: '/alerts',      label: 'Alerts',        icon: AlertTriangle },
];

export default function Sidebar() {
  const path = usePathname();
  const { user, isAuthenticated, isLoading, logout, loginUrl } = useAuth();

  function isActive(href: string) {
    if (href === '/') return path === '/';
    return path.startsWith(href);
  }

  return (
    <aside className="fixed inset-y-0 left-0 z-50 flex w-[260px] flex-col border-r border-white/[0.06] bg-[#060a10]/90 backdrop-blur-2xl">

      {/* Logo */}
      <div className="flex items-center gap-3.5 border-b border-white/[0.06] px-5 py-[22px]">
        <div className="group relative flex h-10 w-10 items-center justify-center overflow-hidden rounded-[10px] border border-cyan-400/20 bg-gradient-to-br from-cyan-500/15 to-emerald-500/10 shadow-lg shadow-cyan-500/10">
          <Rocket className="relative z-10 h-5 w-5 text-cyan-400 transition-transform duration-300 group-hover:rotate-[-15deg] group-hover:scale-110" />
          {/* Shimmer effect */}
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/10 to-transparent translate-x-[-100%] animate-[shimmer_3s_ease-in-out_infinite]" />
        </div>
        <div>
          <p className="text-[15px] font-bold tracking-tight text-white">DeploySense</p>
          <p className="text-[11px] font-medium tracking-wide text-slate-500">Release intelligence</p>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 space-y-1 px-3 py-5">
        {navItems.map(({ href, label, icon: Icon }) => {
          const active = isActive(href);
          return (
            <Link
              key={href}
              href={href}
              className={`group relative flex items-center gap-3 rounded-[10px] border px-3.5 py-2.5 text-[13px] font-medium transition-all duration-200 ${
                active
                  ? 'border-cyan-500/15 bg-gradient-to-r from-cyan-500/10 to-transparent text-white'
                  : 'border-transparent text-slate-400 hover:border-white/[0.06] hover:bg-white/[0.03] hover:text-white'
              }`}
            >
              {/* Active indicator bar */}
              {active && (
                <div className="absolute -left-3 top-1/2 h-6 w-[3px] -translate-y-1/2 rounded-r-full bg-gradient-to-b from-cyan-400 to-emerald-400 shadow-[0_0_12px_rgba(34,211,238,0.3)]" />
              )}

              <div className={`flex h-7 w-7 items-center justify-center rounded-lg transition-colors ${
                active
                  ? 'bg-cyan-500/12'
                  : 'bg-white/[0.04] group-hover:bg-white/[0.06]'
              }`}>
                <Icon className={`h-[15px] w-[15px] transition-colors ${
                  active ? 'text-cyan-400' : 'text-slate-500 group-hover:text-slate-300'
                }`} />
              </div>

              {label}
            </Link>
          );
        })}
      </nav>

      {/* Footer — User profile or Sign in */}
      <div className="border-t border-white/[0.06] px-4 py-4">
        {isLoading ? (
          /* Loading skeleton */
          <div className="flex items-center gap-3 rounded-[10px] border border-white/[0.06] bg-white/[0.02] px-3.5 py-2.5">
            <div className="h-8 w-8 rounded-full bg-white/[0.06] animate-pulse" />
            <div className="flex-1 space-y-1.5">
              <div className="h-3 w-20 rounded bg-white/[0.06] animate-pulse" />
              <div className="h-2.5 w-14 rounded bg-white/[0.04] animate-pulse" />
            </div>
          </div>
        ) : isAuthenticated && user ? (
          /* Authenticated — show user profile */
          <div className="space-y-2">
            <div className="flex items-center gap-3 rounded-[10px] border border-white/[0.06] bg-white/[0.02] px-3.5 py-2.5">
              {user.avatar_url ? (
                <img
                  src={user.avatar_url}
                  alt={user.github_username}
                  className="h-8 w-8 rounded-full border border-white/[0.1] shadow-sm"
                />
              ) : (
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-cyan-500/20 to-emerald-500/15 border border-cyan-500/20">
                  <span className="text-xs font-bold text-cyan-400">
                    {user.github_username.charAt(0).toUpperCase()}
                  </span>
                </div>
              )}
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-white">{user.github_username}</p>
                <p className="truncate text-[11px] text-slate-500">{user.email ?? user.role}</p>
              </div>
            </div>
            <button
              onClick={logout}
              id="sidebar-logout-btn"
              className="flex w-full items-center gap-2.5 rounded-[10px] border border-white/[0.06] bg-white/[0.02] px-3.5 py-2 text-xs font-medium text-slate-400 transition-colors hover:border-red-500/15 hover:bg-red-500/[0.04] hover:text-red-400"
            >
              <LogOut className="h-3.5 w-3.5" />
              Sign out
            </button>
          </div>
        ) : (
          /* Not authenticated — show sign in button */
          <a
            href={loginUrl}
            id="sidebar-github-login-btn"
            className="flex items-center gap-3 rounded-[10px] border border-white/[0.06] bg-white/[0.02] px-3.5 py-2.5 text-sm font-medium text-slate-300 transition-all duration-200 hover:border-white/[0.12] hover:bg-white/[0.05] hover:text-white group"
          >
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-white/[0.06] transition-colors group-hover:bg-white/[0.1]">
              <GithubIcon className="h-4 w-4" />
            </div>
            <span>Sign in with GitHub</span>
          </a>
        )}
      </div>
    </aside>
  );
}
