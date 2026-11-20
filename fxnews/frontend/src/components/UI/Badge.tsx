import type { ReactNode } from 'react'

import { cn } from '~/lib/utils'

export type BadgeTone = 'blue' | 'green' | 'red' | 'yellow' | 'orange' | 'dim'

const toneClasses: Record<BadgeTone, string> = {
  blue: 'border-[var(--accent-dim)] text-[var(--accent)]',
  green: 'border-[var(--green-soft)] text-[var(--green)]',
  red: 'border-[var(--red-soft)] text-[var(--red)]',
  yellow: 'border-[color-mix(in_srgb,var(--yellow)_35%,transparent)] text-[var(--yellow)]',
  orange: 'border-[color-mix(in_srgb,var(--orange)_35%,transparent)] text-[var(--orange)]',
  dim: 'border-[var(--border)] text-[var(--text-secondary)]',
}

type BadgeProps = {
  tone: BadgeTone
  children: ReactNode
  className?: string
  title?: string
}

export const Badge = ({ tone, children, className, title }: BadgeProps) => {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center whitespace-nowrap border px-1.5 py-px text-[calc(10px+var(--font-size-offset))] uppercase leading-4 tracking-[0.12em]',
        toneClasses[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}
