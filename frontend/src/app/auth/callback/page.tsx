'use client';

/**
 * OAuth Callback Page
 *
 * This page handles the redirect back from the GitHub OAuth flow.
 * The backend redirects here with ?token=xxx or ?error=xxx.
 *
 * Flow:
 *   1. Read `token` from URL search params
 *   2. Store it via the auth context
 *   3. Redirect to the dashboard
 */

import { useAuth } from '@/lib/auth';
import { useSearchParams, useRouter } from 'next/navigation';
import { useEffect, useState, Suspense } from 'react';

function CallbackHandler() {
  const { login } = useAuth();
  const searchParams = useSearchParams();
  const router = useRouter();
  const [status, setStatus] = useState<'processing' | 'error'>('processing');
  const [errorMsg, setErrorMsg] = useState('');

  useEffect(() => {
    const token = searchParams.get('token');
    const error = searchParams.get('error');

    if (error) {
      setStatus('error');
      setErrorMsg('Authentication failed. Please try again.');
      return;
    }

    if (!token) {
      setStatus('error');
      setErrorMsg('No authentication token received.');
      return;
    }

    // Store the token and redirect to dashboard
    login(token).then(() => {
      router.replace('/');
    });
  }, [searchParams, login, router]);

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="ds-panel max-w-md w-full mx-4 p-8 text-center">
        {status === 'processing' ? (
          <>
            {/* Animated spinner */}
            <div className="mx-auto mb-6 h-12 w-12 rounded-full border-2 border-cyan-500/20 border-t-cyan-400 animate-spin" />
            <h2 className="text-lg font-semibold text-white mb-2">
              Signing you in…
            </h2>
            <p className="text-sm text-slate-400">
              Completing GitHub authentication
            </p>
          </>
        ) : (
          <>
            {/* Error state */}
            <div className="mx-auto mb-6 flex h-12 w-12 items-center justify-center rounded-full bg-red-500/10 border border-red-500/20">
              <svg className="h-6 w-6 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </div>
            <h2 className="text-lg font-semibold text-white mb-2">
              Authentication Failed
            </h2>
            <p className="text-sm text-slate-400 mb-6">{errorMsg}</p>
            <a
              href="/"
              className="inline-flex items-center gap-2 rounded-lg border border-cyan-500/20 bg-cyan-500/10 px-5 py-2.5 text-sm font-medium text-cyan-400 transition-colors hover:bg-cyan-500/20"
            >
              Return to Dashboard
            </a>
          </>
        )}
      </div>
    </div>
  );
}

export default function AuthCallbackPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center">
          <div className="h-12 w-12 rounded-full border-2 border-cyan-500/20 border-t-cyan-400 animate-spin" />
        </div>
      }
    >
      <CallbackHandler />
    </Suspense>
  );
}
