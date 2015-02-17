import json
import operator
from copy import copy

from django import template
from django.contrib.auth.models import AnonymousUser
from django.template import TemplateSyntaxError
from django.utils.html import escape

import fiber
from fiber.models import Page, ContentItem
from fiber.utils.urls import get_admin_change_url
from fiber.app_settings import PERMISSION_CLASS, AUTO_CREATE_CONTENT_ITEMS
from fiber.utils.import_util import load_class


PERMISSIONS = load_class(PERMISSION_CLASS)

register = template.Library()


class MenuHelper(object):
    """
    Helper class for show_menu tag, for convenience/clarity
    """
    def __init__(self, context, menu_name, min_level=1, max_level=999, expand=None):
        self.context = copy(context)
        self.menu_name = menu_name
        self.min_level = min_level
        self.max_level = max_level
        self.expand = expand
        self.menu_parent = None

    def filter_min_level(self, menu_pages):
        """
        Remove pages that are below the minimum_level
        """
        # Page.get_absolute_url() accesses self.parent recursively to build URLs (assuming relative URLs).
        # To render any menu item, we need all the ancestors up to the root.
        # It is more efficient to fetch the entire tree, and apply min_level manually.
        return [p for p in menu_pages if p.level >= self.min_level]

    def filter_for_user(self, menu_pages):
        """
        Remove pages that shouldn't be shown in the menu for the current user.
        """
        user = self.context.get('user', AnonymousUser())
        return [p for p in menu_pages if p.show_in_menu and p.is_public_for_user(user)]

    def get_context_data(self):
        return {
            'Page': Page,
            'fiber_menu_pages': self.get_menu(),
            'fiber_menu_parent_page': self.menu_parent,
            'fiber_menu_args': {
                'menu_name': self.menu_name,
                'min_level': self.min_level,
                'max_level': self.max_level,
                'expand': self.expand
            }
        }

    def get_root(self):
        try:
            return Page.objects.get(title=self.menu_name, parent=None)
        except Page.DoesNotExist:
            raise Page.DoesNotExist("Menu does not exist.\nNo top-level page found with the title '%s'." % self.menu_name)

    def get_menu(self):
        root = self.get_root()
        current = self.context.get('fiber_page')

        if current and current.is_child_of(root):
            needed_pages = self.get_menu_for_current_page(root, current)
        else:
            # Only show menus that start at the first level (min_level == 1)
            # when the current page is not in the menu tree.
            needed_pages = []
            if self.min_level == 1:
                if not self.expand:
                    needed_pages = Page.objects.filter(tree_id=root.tree_id).filter(level__lte=1)
                elif self.expand == 'all':
                    needed_pages = Page.objects.filter(tree_id=root.tree_id).filter(level__lte=self.max_level)

        menu_pages = Page.objects.link_parent_objects(needed_pages)
        menu_pages = self.filter_min_level(menu_pages)
        menu_pages = self.filter_for_user(menu_pages)

        # Order menu_pages for use with tree_info template tag.
        menu_pages.sort(key=operator.attrgetter('lft'))

        # Set the parent page for this menu
        self.menu_parent = None
        if menu_pages:
            self.menu_parent = menu_pages[0].parent
        elif self.min_level == 1:
            self.menu_parent = root

        return menu_pages

    def get_menu_for_current_page(self, root, current):
        tree = root.get_descendants(include_self=True).filter(level__lte=self.max_level)
        if self.expand == 'all':
            needed_pages = tree
        else:
            if current.level + 1 < self.min_level:
                # Nothing to do
                needed_pages = []
            else:
                # We need the 'route' nodes, the 'sibling' nodes and the children
                route = tree.filter(lft__lt=current.lft, rght__gt=current.rght)

                # We show any siblings of anything in the route to the current page.
                # The logic here is that if the user drills down, menu items
                # shown previously should not disappear.

                # The following assumes that accessing .parent is cheap, which
                # it can be if current_page was loaded correctly.
                p = current
                sibling_qs = []
                while p.parent_id is not None:
                    sibling_qs.append(tree.filter(level=p.level, lft__gt=p.parent.lft, rght__lt=p.parent.rght))
                    p = p.parent
                route_siblings = reduce(operator.or_, sibling_qs)

                children = tree.filter(lft__gt=current.lft, rght__lt=current.rght)
                if self.expand != 'all_descendants':
                    # only want immediate children:
                    children = children.filter(level=current.level + 1)

                needed_pages = route | route_siblings | children
        return needed_pages


@register.inclusion_tag('fiber/menu.html', takes_context=True)
def show_menu(context, menu_name, min_level, max_level, expand=None):
    context = copy(context)
    context.update(MenuHelper(context, menu_name, min_level, max_level, expand).get_context_data())
    return context


@register.inclusion_tag('fiber/content_item.html', takes_context=True)
def show_content(context, content_item_name):
    content_item = None
    try:
        content_item = ContentItem.objects.get(name__exact=content_item_name)
    except ContentItem.DoesNotExist:
        if AUTO_CREATE_CONTENT_ITEMS:
            content_item = ContentItem.objects.create(name=content_item_name)

    context['content_item'] = content_item

    return context


@register.inclusion_tag('fiber/content_items.html', takes_context=True)
def show_page_content(context, page_or_block_name, block_name=None):
    """
    Fetch and render named content items for the current fiber page, or a given fiber page.

    {% show_page_content "block_name" %}              use fiber_page in context for content items lookup
    {% show_page_content other_page "block_name" %}   use other_page for content items lookup
    """
    if isinstance(page_or_block_name, basestring) and block_name is None:
        # Single argument e.g. {% show_page_content 'main' %}
        block_name = page_or_block_name
        try:
            page = context['fiber_page']
        except KeyError:
            raise TemplateSyntaxError("'show_page_content' requires 'fiber_page' to be in the template context")
    elif isinstance(page_or_block_name, Page) and block_name:
        # Two arguments e.g. {% show_page_content other_page 'main' %}
        page = page_or_block_name
    else:
        # Bad arguments
        raise TemplateSyntaxError("'show_page_content' received invalid arguments")

    page_content_items = page.page_content_items.filter(block_name=block_name).order_by('sort').select_related('content_item')
    content_items = []
    for page_content_item in page_content_items:
        content_item = page_content_item.content_item
        content_item.page_content_item = page_content_item
        content_items.append(content_item)

    context = copy(context)
    context.update({
        'fiber_page': page,
        'ContentItem': ContentItem,
        'fiber_block_name': block_name,
        'fiber_content_items': content_items
    })
    return context


@register.tag(name='captureas')
def do_captureas(parser, token):
    try:
        tag_name, args = token.contents.split(None, 1)
    except ValueError:
        raise template.TemplateSyntaxError("'captureas' node requires a variable name.")

    nodelist = parser.parse(('endcaptureas',))
    parser.delete_first_token()

    return CaptureasNode(nodelist, args)


class CaptureasNode(template.Node):

    def __init__(self, nodelist, varname):
        self.nodelist = nodelist
        self.varname = varname

    def render(self, context):
        output = self.nodelist.render(context)
        context[self.varname] = output
        return ''


def get_editable_attrs(instance):
    data = {
        "url": get_admin_change_url(instance),
    }

    return "data-fiber-data='%s'" % json.dumps(data)


class EditableAttrsNode(template.Node):

    def __init__(self, instance_var):
        self.instance_var = template.Variable(instance_var)

    def render(self, context):
        try:
            instance = self.instance_var.resolve(context)
            return get_editable_attrs(instance)
        except template.VariableDoesNotExist:
            return ''


@register.tag(name='editable_attrs')
def editable_attrs(parser, token):
    try:
        instance_var = token.split_contents()[1]
    except ValueError:
        raise template.TemplateSyntaxError, "%r tag requires one argument" % token.contents.split()[0]

    return EditableAttrsNode(instance_var)


@register.filter
def escape_json_for_html(value):
    """
    Escapes valid JSON for use as a HTML attribute value
    """
    return escape(value)


@register.filter
def can_edit(obj, user):
    return PERMISSIONS.can_edit(user, obj)


@register.simple_tag
def fiber_version():
    return fiber.__version__
