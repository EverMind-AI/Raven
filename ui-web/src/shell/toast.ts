/* The transient notices appended to the page's standing #toasts host. */

export interface ToastAction {
  label: string
  fn: () => void
}

export function show(text: string, action?: ToastAction): void {
  const host = document.getElementById('toasts')
  if (!host) return
  const box = document.createElement('div')
  box.className = 'toast'
  const message = document.createElement('span')
  message.className = 't'
  message.textContent = text
  box.appendChild(message)
  if (action) {
    const button = document.createElement('button')
    button.textContent = action.label
    button.onclick = () => {
      action.fn()
      box.remove()
    }
    box.appendChild(button)
  }
  host.appendChild(box)
  window.setTimeout(() => box.remove(), action ? 5200 : 2600)
}
