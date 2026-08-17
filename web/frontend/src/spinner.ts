// [SPIN-1] Same phrase list as cli/ui.py / tui/app.py's LOADING_PHRASES,
// ported verbatim from the old static index.html so a pending bubble reads
// "beavering away" instead of sitting empty. The visual spinner itself is
// now framer-motion driven in Spinner.tsx (SPIN-2) — braille frame data
// used to live here too but is gone now that nothing cycles text frames.

export const LOADING_PHRASES = [
  "working on it", "hold on", "doing stuff", "computing things",
  "probably fine", "this might take a sec", "not frozen, promise",
  "grepping for answers", "spawning subprocesses", "piping to /dev/brain",
  "forking the timeline", "diffing reality", "cat-ing the void",
  "chmod 777 your request", "segfault: just kidding", "killing -9 uncertainty",
  "bribing the model", "consulting the beaver council",
  "chewing through your request", "negotiating with neurons",
  "the tokens are scared", "yelling into the context window",
  "manifesting an answer", "asking nicely", "vibing computationally",
  "staring at your query", "arguing with the graph",
  "summoning output from thin air", "debugging the universe",
  "rebooting reality", "optimizing", "waiting for inspiration",
  "consulting the oracle", "asking the magic 8-ball",
  "looking for the silver lining",
  "sharpening my teeth on this", "building a dam of context",
  "gnawing through the problem", "checking the context window twice",
  "reticulating splines", "asking the rubber duck",
  "counting to infinity, slowly", "convincing the GPU to cooperate",
  "un-fumbling the tokens", "assembling a coherent thought",
  "taming a stray semicolon", "buffering existential dread",
  "polling the universe for updates", "measuring twice, cutting never",
  "downloading more RAM", "aligning the stars and the tensors",
  "whispering to the vector store", "double-checking with myself",
  "letting the KV cache breathe", "consulting past me",
  "sniffing out the answer", "collecting my thoughts (literally)",
  "walking the graph", "waiting on the model, not the coffee",
  "beavering away", "gnashing on gigabytes",
];

export function randomPhrase(): string {
  return LOADING_PHRASES[Math.floor(Math.random() * LOADING_PHRASES.length)];
}