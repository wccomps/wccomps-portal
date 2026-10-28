# Cotton Component Library

Reusable UI components for WCComps Portal templates using Django Cotton.

## Components

### Layout & Structure

#### `<c-module>`
Content container (card-like) with optional toolbar.
```django
<c-module>
    <c-slot name="toolbar">
        <c-link href="/back">Back</c-link>
    </c-slot>
    Main content here
</c-module>
```

#### `<c-detail_grid>` and `<c-detail_row>`
Label/value pairs for a record's details.
```django
<c-detail_grid>
    <c-detail_row label="Status">Open</c-detail_row>
</c-detail_grid>
```

#### `<c-button_row>`
A row of buttons and links that ends a section: after its text, form or detail grid, never inside the grid
(`core/tests/test_template_vocabulary.py` fails on one there). Every `<c-link>` inside a button row must set a
button `variant` (`primary`, `secondary`, `danger`, `warning`, or `cancel`). A row sits inside its module with no box of its own;
`boxed="true"` gives it a grey bar, for a row that is the whole section (the team page's Actions).
```django
<c-button_row class="justify-end">
    <c-link href="/back" variant="cancel">Cancel</c-link>
    <c-button type="submit" variant="primary">Save</c-button>
</c-button_row>
```

#### `<c-page_header>`
Page title with optional subtitle.
```django
<c-page_header title="Ticket List" subtitle="Team Blue" />
```

#### `<c-fieldset>`
Grouped form fields with heading.
```django
<c-fieldset heading="Ticket Information">
    <c-form_field label="Title" required="true">
        <input type="text" name="title" />
    </c-form_field>
</c-fieldset>
```

### Forms

#### `<c-form_errors>`
The errors of the form as a whole (raised in its `clean()`), which no field shows. Every page that renders a form
field by field puts one at the top of its form (`core/tests/test_form_conventions.py` fails on one without).
```django
<c-form_errors :form="form" />
```

#### `<c-form_field>`
Form row with label, input, and optional help text/error.
```django
<c-form_field label="Email" required="true" help_text="Enter your work email">
    <input type="email" name="email" />
</c-form_field>
```

#### `<c-filter_toolbar>`
Search and filter form with horizontal layout.
```django
<c-filter_toolbar form_id="ticket-filters">
    <c-filter_field label="Status" id="id_status">
        <select name="status">...</select>
    </c-filter_field>
</c-filter_toolbar>
```

#### `<c-filter_field>`
Individual filter input within toolbar.
```django
<c-filter_field label="Search" id="id_search">
    <input type="text" name="search" />
</c-filter_field>
```

#### `<c-approval_filter>` and `<c-team_filter>`
The review lists' approval-status filter and a list's team filter, which names a team by its number. The views
filter with `core.utils.by_approval` and `by_team`.
```django
<c-approval_filter value="{{ filters.status }}" />
<c-team_filter :numbers="team_numbers" value="{{ chosen_team|default_if_none:'' }}" />
```

### Interactive Elements

#### `<c-button>`
Styled action buttons.
```django
<c-button variant="primary" type="submit">Save</c-button>
<c-button variant="danger">Delete</c-button>
<c-button variant="cancel">Cancel</c-button>
```

Props:
- `variant`: default|primary|secondary|danger|warning|cancel
- `type`: button|submit
- `class`, `id`, `name`, `value`

#### `<c-toggle_button>`
A button that stays pressed while its choice is picked (`pressed="true"` for the initial state); the page's
script calls `setTogglePressed(button, pressed)` from `static/js/utils.js`, which sets its look and `aria-pressed`.
```django
<c-toggle_button data-box="web01">web01</c-toggle_button>
```

#### `<c-link>`
Styled links.
```django
<c-link href="/back">Back</c-link>
<c-link href="/add" variant="add">Add New</c-link>
```

Props: `href`, `variant` (default|primary|secondary|add|change|danger|warning|cancel)

### Display

#### `<c-badge>`
Status indicators with color coding.
```django
<c-badge variant="info">Open</c-badge>
<c-badge variant="warning">Claimed</c-badge>
<c-badge variant="success">Resolved</c-badge>
<c-badge variant="neutral">Cancelled</c-badge>
<c-badge variant="{{ ticket.status|badge_variant:'ticket' }}">{{ ticket.get_status_display }}</c-badge>
```

#### `<c-alert>`
Info, warning, error, success messages.
```django
<c-alert variant="error">Something went wrong</c-alert>
<c-alert variant="warning">Points will be deducted</c-alert>
<c-alert variant="success">Ticket created</c-alert>
```

#### `<c-score_value>`
Auto-colored score display with optional signed format.
```django
<c-score_value value='150' />           <!-- Green for positive -->
<c-score_value value='-25' />           <!-- Red for negative -->
<c-score_value value='0' />             <!-- Gray for zero -->
<c-score_value value='50' format='signed' />  <!-- Shows "+50" in green -->
<c-score_value :value="score" />        <!-- From template variable -->
```

Props:
- `value` (required): Numeric value to display
- `format` (optional): "default" (no + prefix) or "signed" (adds + to positive)

Color mapping:
- Positive values: #28a745 (green)
- Negative values: #dc3545 (red)
- Zero: #6c757d (gray)

#### `<c-empty_state>`
Empty state display for when no data is available.
```django
<c-empty_state title="No incidents found" />
<c-empty_state title="No tickets yet" description="Tickets your team opens show here.">
    <c-link href="{% url 'create_ticket' %}" variant="add">Create your first ticket</c-link>
</c-empty_state>
```

Props:
- `title` (required): Main heading text
- `description` (optional): Secondary explanatory text in muted color
- Slot: Optional action button or link

Use cases: an empty list. A list its filters empty uses `<c-no_matches>`.

#### `<c-no_matches>`
The empty state of a list whose filters hide every row, with a link that clears them.
```django
<c-no_matches items="tickets" href="{% url 'ticket_list' %}" />
```

### Tables

#### `<c-table>`
Data table with header and body slots.
```django
<c-table id="results" aria_label="Tickets list" fixed_layout="true">
    <c-slot name="headers">
        <tr>
            <c-table_header sort_field="team" current_sort="-created_at">Team</c-table_header>
        </tr>
    </c-slot>
    <tr><td>Data row</td></tr>
</c-table>
```

Props: `id`, `class`, `aria_label`, `role` (e.g. `grid` for a table whose cells are `gridcell`s), `fixed_layout`,
`widths` (column widths, space separated: `widths="40px 20% 30%"`; template tags may pick them).
Slot: `headers`.

#### `<c-table_header>`
Sortable table column header.
```django
<c-table_header
    sort_field="team__team_number"
    current_sort="{{ sort_by }}">
    Team Number
</c-table_header>
```

#### `<c-pagination>`
Page navigation for paginated results.
```django
<c-pagination :page_obj="page_obj" />
```

Both build their links with Django's `{% querystring %}`, so a link keeps the page's query (its filters) and changes
only `sort` or `page`. A list's own refresh does the same (`hx-get="{% url 'ticket_list' %}{% querystring %}"`); a
link from another page back to a list doesn't, since it would carry that page's query.

## Benefits

1. **Consistency**: UI elements look and behave the same across pages
2. **Maintainability**: Update styling in one place, affects all uses
3. **Reduced Code**: Less repetition in templates (~40% reduction)
4. **Documentation**: Self-documenting with clear prop names

## Attributes and Escaping

A component writes the attributes it doesn't declare in `<c-vars>` with `{{ attrs }}`. django-cotton doesn't escape
them; `core/cotton.py` does, for every component:
- a safe string only has `"` encoded: a value with template tags (`title="{{ search }}"`, which the engine already
  escaped, or `href="?a=1&amp;b={{ x }}"`), a quoted literal (`:title="'text'"`, template source, which Django marks
  safe as it does `{{ "text" }}`), or a value a view marked safe;
- any other value is escaped: from the page's context (`:title="search"`, `title=search`, `:attrs="dict"`) or a
  plain literal (`title="Tom & Jerry"`). In a plain literal write the character, not an entity (a template lint
  fails on `&amp;` or `&quot;` there); quote the attribute with `'` when the value holds `"`.

An Alpine binding on a component is written `::name` (cotton passes it through as `:name`). A `:name` that isn't in
the page's context (and isn't a Python literal such as `0`) fails the render under DEBUG and in tests
(`STRICT_COMPONENT_ATTRS`), on any component; in production it is reported and the component renders as if it
wasn't passed (its `<c-vars>` default, if it has one).

`core/tests/test_cotton_escaping.py` renders components with values built to break out of their attribute and fails
if one comes out raw; run it after any django-cotton upgrade, since `core/cotton.py` replaces three of its internals
(`Attrs.__str__`, `Attrs.unprocessable` and the component node's `render`). Escaping makes a value safe as attribute text, not as a URL or code: request text reaches an `href` only as
a query value `{% querystring %}` encodes, or as the browser's own `location.search` (the ticket page's way back),
and never an `on*` or Alpine attribute.

One page's markup that htmx also refreshes (a list's table) is a template beside the page that the page
`{% include %}`s and the view renders on its own, not a component.

## Styling Vocabulary

A page styles itself only with classes: the ones these components render (with their app.css modifiers, such as
`alert--error`), the utilities between `BEGIN UTILITIES` and `END UTILITIES` in `static/css/app.css`, and Django
admin's classes. A class the page's own script adds, or a selector that only mentions a component, is the page's
own. No `style` attributes or `<style>` blocks: a fixed look is a utility, a choice is a component option (such as
`<c-table widths="...">`). Markup a component renders goes through the component, a `variant` is one the component
knows, and a stored status reaches a badge through `|badge_variant:"kind"` (`core/status_variants.py`). A
preview of long text is `|shorten:N` (`core.utils.shorten`), never `truncatechars` or `truncatewords`, which can leave
a parenthesis open (`core/tests/test_template_lint.py` fails on them).
`core/tests/test_template_vocabulary.py` enforces all of this, and fails on an app.css rule no page uses; each
check's exceptions list says why no component or utility fits.

## Migration Strategy

Templates can be gradually refactored to use components:
1. Replace repetitive button HTML with `<c-button>`
2. Replace status spans with `<c-badge>`
3. Replace form rows with `<c-form_field>`
4. Replace tables with `<c-table>` and `<c-table_header>`

The components use Django admin's CSS and app.css.
