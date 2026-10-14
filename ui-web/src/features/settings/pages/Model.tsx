/* Model settings: which model each job runs on. The accounts those models come
   from are the Model providers page -- "what do I have" and "how do I spend it"
   are two questions, and one page answering both buried the second.

   A memory backend's credential is neither question, so the EverOS Cloud card
   is deliberately absent (providers/CloudCard.tsx): it landed here only because
   it stood in for the three EverOS role rows the cloud hides, and standing in
   for a model row is not the same as being one. Where it belongs is the Memory
   section's to say. */
import { Roles } from '../providers/Roles'

import type { JSX } from 'react'

export function Model(): JSX.Element {
  return <Roles />
}
