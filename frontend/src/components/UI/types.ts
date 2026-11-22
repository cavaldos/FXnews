export type ButtonTone = 'primary' | 'ghost' | 'danger'
export type ButtonSize = 'xs' | 'sm' | 'md'

export type SelectOption = {
  value: string
  label: string
  /** Optional group heading; consecutive options with the same group are listed under one header. */
  group?: string
  /** Native tooltip. */
  title?: string
  /** Shown but not selectable. */
  disabled?: boolean
}
