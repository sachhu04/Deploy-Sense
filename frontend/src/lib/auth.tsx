'use client';

/**
 * DeploySense — Authentication Context
 *
 * Provides auth state (user, token, loading) to the entire app.
 * Stores JWT in localStorage for persistence across page refreshes.
 *
 * Usage:
 *   const { user, isAuthenticated, login, logout } = useAuth();
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';

// ── Types ────────────────────────────────────────────────────────────────────

export interface AuthUser {
  id: string;
  github_username: string;
  email: string | null;
  avatar_url: string | null;
  role: string;
}

interface AuthContextType {
  user: AuthUser | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (token: string) => Promise<void>;
  logout: () => void;
  loginUrl: string;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

// ── Constants ────────────────────────────────────────────────────────────────

const TOKEN_KEY = 'deploysense_token';
const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? '';

// ── Provider ─────────────────────────────────────────────────────────────────

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // Fetch user profile from JWT
  const fetchUser = useCallback(async (jwt: string): Promise<AuthUser | null> => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/auth/me`, {
        headers: { Authorization: `Bearer ${jwt}` },
        cache: 'no-store',
      });
      if (!res.ok) return null;
      return (await res.json()) as AuthUser;
    } catch {
      return null;
    }
  }, []);

  // Initialize auth state from localStorage on mount
  useEffect(() => {
    const stored = localStorage.getItem(TOKEN_KEY);
    if (!stored) {
      setIsLoading(false);
      return;
    }

    fetchUser(stored).then((u) => {
      if (u) {
        setToken(stored);
        setUser(u);
      } else {
        // Token expired or invalid — clean up
        localStorage.removeItem(TOKEN_KEY);
      }
      setIsLoading(false);
    });
  }, [fetchUser]);

  // Login: store token and fetch user
  const login = useCallback(
    async (jwt: string) => {
      localStorage.setItem(TOKEN_KEY, jwt);
      setToken(jwt);
      const u = await fetchUser(jwt);
      setUser(u);
    },
    [fetchUser],
  );

  // Logout: clear everything
  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setToken(null);
    setUser(null);
  }, []);

  // The URL that initiates the GitHub OAuth flow (via backend redirect)
  const loginUrl = `${API_BASE}/api/v1/auth/github/login`;

  const value = useMemo<AuthContextType>(
    () => ({
      user,
      token,
      isAuthenticated: !!user,
      isLoading,
      login,
      logout,
      loginUrl,
    }),
    [user, token, isLoading, login, logout, loginUrl],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// ── Hook ─────────────────────────────────────────────────────────────────────

export function useAuth(): AuthContextType {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return ctx;
}
