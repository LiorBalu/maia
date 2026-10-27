import clsx from 'clsx'

const tones = [
  'bg-brand-100 text-brand-800',
  'bg-sky-100 text-sky-800',
  'bg-cyan-100 text-cyan-800',
  'bg-teal-100 text-teal-800',
  'bg-indigo-100 text-indigo-800',
]

function toneFor(seed: string): string {
  let hash = 0
  for (const ch of seed) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0
  return tones[hash % tones.length]
}

/** Initial-letter avatar with a stable color per name. */
export function Avatar({ name, size = 'md' }: { name: string; size?: 'sm' | 'md' | 'lg' }) {
  const initial = (name.trim().charAt(0) || '?').toUpperCase()
  return (
    <div
      className={clsx(
        'grid shrink-0 place-items-center rounded-full font-semibold',
        toneFor(name),
        size === 'sm' && 'h-8 w-8 text-xs',
        size === 'md' && 'h-10 w-10 text-sm',
        size === 'lg' && 'h-14 w-14 text-lg',
      )}
      aria-hidden
    >
      {initial}
    </div>
  )
}
