import type { ButtonHTMLAttributes, ReactNode } from 'react'

import { cn } from '~/lib/utils'

import type { ButtonSize, ButtonTone } from './types'

const toneClasses: Record<ButtonTone, string> = {
  primary:
    'border-[var(--accent-dim)] text-[var(--accent)] enabled:hover:border-[var(--accent)] enabled:hover:bg-[var(--accent-glow)] enabled:hover:shadow-[0_0_16px_var(--accent-glow),inset_0_0_16px_var(--accent-glow)]',
  ghost:
    'border-[var(--border)] text-[var(--text-secondary)] enabled:hover:border-[var(--border-bright)] enabled:hover:text-[var(--text-primary)]',
  danger:
    'border-[var(--red-soft)] text-[var(--red)] enabled:hover:border-[var(--red)] enabled:hover:bg-[var(--red-muted)] enabled:hover:shadow-[0_0_12px_var(--red-shadow)]',
}

const sizeClasses: Record<ButtonSize, string> = {
  xs: 'px-2 py-1 text-[calc(10px+var(--font-size-offset))] tracking-[0.14em]',
  sm: 'px-3 py-1.5 text-[calc(11px+var(--font-size-offset))] tracking-[0.16em]',
  md: 'px-5 py-2.5 text-[calc(12px+var(--font-size-offset))] tracking-[0.2em]',
}

type UiButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  tone?: ButtonTone
  size?: ButtonSize
  children: ReactNode
}

export const UiButton = ({ tone = 'primary', size = 'md', children, className, type = 'button', ...props }: UiButtonProps) => {
  return (
    <button
      type={type}
      className={cn(
        'inline-flex items-center justify-center gap-1.5 whitespace-nowrap border bg-transparent font-[var(--mono)] uppercase transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-50',
        toneClasses[tone],
        sizeClasses[size],
        className,
      )}
      {...props}
    >
      {children}
    </button>
  )
}
