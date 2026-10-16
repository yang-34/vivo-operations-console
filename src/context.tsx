import { createContext,useContext } from 'react'
import type { Mode } from './api'
export type WorkspaceValue={mode:Mode;refresh:number;reload:()=>void;navigate:(route:string)=>void}
export const WorkspaceContext=createContext<WorkspaceValue>(null!)
export const useWorkspace=()=>useContext(WorkspaceContext)
