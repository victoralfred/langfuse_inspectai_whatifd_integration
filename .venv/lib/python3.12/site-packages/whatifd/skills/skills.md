# Adding New Skills

A skill is a declarative definition that describes a specific action or behavior that an agent can perform. Skills are defined using a declarative syntax, which allows for easy readability and maintainability.
Write a `skill.md` file, which will contain the declarative definition of the skill.

---

## The quick path: `whatifd skill <name>`

```
skills/
  <name>/
    skill.md  <- you write this
  generate.py <- runs automatically to generate the skill code via `whatifd skill <name>`
```

The generator:
1. Reads and validates your `skill.md`
2. Generates the skill code based on the declarative definition
3. Writes the generated skill code to a file named `<name>.py` in the `<name>` directory
4. Validates the generated skill code for correctness and adherence to the declarative definition
5. Registers the skill with the agent's skill registry for immediate use
6. Provides feedback on the success or failure of the skill registration process
7. Logs the registration event for auditing and debugging purposes
 optimizations

### Behind the scene
1. Calls MCP server or AItool (e.g Claude/GPT) to write `src/whatifd/adapters/<name>/__init__.py`
2. Patches `src/whatifd/config.py` - adds fields to the datatype schema and loader
3. Patches `src/whatifd/runner/factory.py` - adds the import and conditional `registry.register()`
4. Appends to `env.example`
5. Runs test and reports pass/fail
---


## Step 1 - Write `src/skills/<name>/skill.md`

Create a directory and a `skill.md` with YAML frontmatter. The frontmatter is what the generator parses; the AI tool takes over to parse the markdown body to implement the context.
