import { useCallback, useEffect, useRef, useState } from 'react'

export type PopupResult = { ok: true; warning?: string } | { ok: false; message: string }

/**
 * Opens Google's consent screen in a popup. The backend callback page posts
 * `auth_success` (string or `{type:'auth_success', warning}`) or `{type:'auth_error', message}`
 * to the opener and closes itself; a closed popup without a message counts as cancelled.
 */
export function useGooglePopup(onDone: (result: PopupResult) => void) {
  const [busy, setBusy] = useState(false)
  const popupRef = useRef<Window | null>(null)
  const doneRef = useRef(onDone)
  doneRef.current = onDone

  useEffect(() => {
    function onMessage(event: MessageEvent) {
      const data = event.data
      const type = typeof data === 'string' ? data : (data as { type?: string } | null)?.type
      if (type === 'auth_success') {
        setBusy(false)
        doneRef.current({ ok: true, warning: (data as { warning?: string })?.warning })
      } else if (type === 'auth_error') {
        setBusy(false)
        doneRef.current({ ok: false, message: (data as { message?: string })?.message ?? '' })
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])

  const open = useCallback((url: string) => {
    const width = 520
    const height = 640
    const left = window.screenX + (window.outerWidth - width) / 2
    const top = window.screenY + (window.outerHeight - height) / 2
    const popup = window.open(url, 'maia-google-auth', `width=${width},height=${height},left=${left},top=${top}`)
    if (!popup) {
      doneRef.current({ ok: false, message: 'popup-blocked' })
      return
    }
    popupRef.current = popup
    setBusy(true)
    const timer = window.setInterval(() => {
      if (popup.closed) {
        window.clearInterval(timer)
        setBusy((wasBusy) => {
          if (wasBusy) doneRef.current({ ok: false, message: 'closed' })
          return false
        })
      }
    }, 700)
  }, [])

  return { open, busy }
}
