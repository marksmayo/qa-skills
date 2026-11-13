// Package script evaluates the small expressions a flow file may contain.
package script

// Value is a result the interpreter hands back.
type Value interface{ String() string }

// Runtime is the embedded interpreter.
type Runtime interface {
	Get(name string) Value
	Set(name string, v any) error
}

// Options are the browser launch options a flow may set.
type Options interface{ Get(key string) string }

// Output reads the value a script left in its output variable.
func Output(rt Runtime) string {
	return rt.Get("output").String()
}

// ProfileDir is the browser profile directory a flow asked for.
func ProfileDir(o Options) string {
	return o.Get("user-data-dir")
}
