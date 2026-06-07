import { create } from 'zustand'

export interface ChatMessage {
  id?: number
  role: 'user' | 'assistant'
  content: string
  intent?: string
  cost_usd?: number
  streaming?: boolean
}

interface ChatState {
  sessionId: string | null
  messages: ChatMessage[]
  loading: boolean
  setSessionId: (id: string | null) => void
  addMessage: (msg: ChatMessage) => void
  appendToken: (token: string) => void
  setLoading: (v: boolean) => void
  clearMessages: () => void
}

export const useChatStore = create<ChatState>((set) => ({
  sessionId: null,
  messages: [],
  loading: false,
  setSessionId: (id) => set({ sessionId: id }),
  addMessage: (msg) => set((s) => ({ messages: [...s.messages, msg] })),
  appendToken: (token) =>
    set((s) => {
      const msgs = [...s.messages]
      const last = msgs[msgs.length - 1]
      if (last && last.role === 'assistant' && last.streaming) {
        msgs[msgs.length - 1] = { ...last, content: last.content + token }
      } else {
        msgs.push({ role: 'assistant', content: token, streaming: true })
      }
      return { messages: msgs }
    }),
  setLoading: (v) => set({ loading: v }),
  clearMessages: () => set({ messages: [], sessionId: null }),
}))
