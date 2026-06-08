import type { Metadata } from 'next';
import { Inter, JetBrains_Mono } from 'next/font/google';
import './globals.css';
import Sidebar from '@/components/layout/Sidebar';
import { AuthProvider } from '@/lib/auth';

const inter = Inter({
  subsets: ['latin'],
  variable: '--font-inter',
  display: 'swap',
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ['latin'],
  variable: '--font-mono',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'DeploySense — Deployment Intelligence Platform',
  description: 'Predict deployment risk, track releases in real-time, and maintain system stability with AI-powered insights.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${jetbrainsMono.variable}`}>
      <body>
        <AuthProvider>
          <div className="relative flex min-h-screen">
            <Sidebar />
            <div className="relative z-10 ml-[260px] flex flex-1 flex-col">
              {children}
            </div>
          </div>
        </AuthProvider>
      </body>
    </html>
  );
}

