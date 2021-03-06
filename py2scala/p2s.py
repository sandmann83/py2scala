'''p2s -- convert python lexical syntax to scala

Use python ast__ module to traverse a python module and convert to scala.

see also batteries.scala runtime support

ideas:
 - distinguish "not implemented" from "not possible/feasible" in limitation()
 - more general handling of x[y:z]
 - option to allow PyObject ~= scala Dynamic, a little like untyped in haxe

__ http://docs.python.org/2/library/ast.html

'''

from contextlib import contextmanager
from os.path import splitext, basename
import StringIO
import ast
import logging
import re
import tokenize
from ast import copy_location as loc

from fp import option_iter, option_fold, partition

log = logging.getLogger(__name__)


def main(argv, open, stdout, find_package,
         level=logging.INFO):
    logging.basicConfig(level=logging.DEBUG if '--debug' in argv else level)
    [pkg, infn] = (argv[2:4] if ['--package'] == argv[1:2]
                   else (None, argv[1]))
    api = '--api' in argv
    convert(infn, open(infn).read(), stdout, find_package,
            pkg=None,
            api=api)


def convert(infn, src, out, find_package,
            pkg=None,
            api=False):
    modname = splitext(basename(infn))[0]
    t = ast.parse(src, infn)
    tl = PyToScala.tokens_per_line(src)
    p2s = PyToScala(modname, out, tl,
                    find_package=lambda n, lvl=0: find_package(infn, n, lvl),
                    pkg=pkg, api=api)
    p2s.visit(t)


class LineSyntax(object):
    def __init__(self, out, token_lines):
        self._out = out
        self._lines = list(token_lines)
        self._col = 0
        self._line_ix = 0

    @classmethod
    def tokens_per_line(cls, src):
        '''Enumerate lines and the tokens they contain.

        :rtype: Iterator[(Int, Seq[Token])]
        '''
        row = 1
        line = []
        readline = StringIO.StringIO(src).readline
        for tok in tokenize.generate_tokens(readline):
            toknum, tokval, (tokrow, col), rc, l = tok
            if tokrow > row:
                yield (row, line)
                line = []
                row = tokrow
            line.append(tok)
        if line:
            yield (row, line)

    def _sync(self, node):
        wr = self._out.write
        if isinstance(node, ast.expr) or isinstance(node, ast.stmt):
            while node.lineno >= self._lines[self._line_ix][0]:
                for tok in self._lines[self._line_ix][1]:
                    if tok[0] == tokenize.COMMENT:
                        wr('//' + tok[1][1:])
                        self.newline()
                self._line_ix += 1
        return wr

    @contextmanager
    def _block(self):
        wr = self._out.write
        self._col += 2
        wr('{\n')
        wr(' ' * self._col)
        yield
        self._col -= 2
        wr('}\n')
        wr(' ' * self._col)

    def newline(self):
        wr = self._out.write
        wr('\n')
        wr(' ' * self._col)


class ModuleAttributes(LineSyntax):
    '''Predefined module attributes, per `python datamodel`__.

    __ http://docs.python.org/2/reference/datamodel.html
    '''
    def mod_attrs(self, wr, pkg, modname):
        wr('val __name__ = "%s%s"' % (
            pkg + '.' if pkg else '', modname))
        self.newline()


class PyRunTime(object):
    scala_rt = []
    list_maker = '`[...]`'

    def __init__(self, find_package, batteries_pfx, py2scala):
        self._find_package = find_package
        self._batteries_pfx = batteries_pfx
        self._py_rt_imports = [
            '%s.{batteries => %s}' % (py2scala, batteries_pfx),
            '%s.__builtin__._' % py2scala]

    def adjust_pkg_path(self, pkg_path, level=0):
        is_std, is_local, path_parts = self._find_package(pkg_path, level)
        # limitation(is_std or is_local)
        return (([self._batteries_pfx] if is_std else [])
                + path_parts)

    def imports(self):
        '''Get list of scala and python runtime imports.
        '''
        return self.scala_rt + self._py_rt_imports


class ImplementationDetail(Exception):
    pass


class APIFilter(object):
    '''Optionally filter out implementation details, leaving only the API.

    In API mode:
       - Skip _xyz functions.
       - Leave body empty in the rest.
    '''
    # TODO: investigate whether we can do this in a generic
    # way in NodeVisitor.visit.
    # Or consider using: for do_impl in self.filter_fundef():
    def __init__(self, api):
        self._api = api

    def check_fun_name(self, name):
        if self._api and name.startswith('_'):
            raise ImplementationDetail

    def filter_funbody(self):
        if self._api:
            raise ImplementationDetail


class TypeDecls(object):
    def __init__(self):
        self._def_stack = []

    @classmethod
    def parse_types(cls, txt):
        '''Parse a mixure of Spinx typing conventions and scala syntax.

        Parameter types are written in scala syntax::

          >>> TypeDecls.parse_types(':type x: String')
          ([('x', 'String')], None, '')

        Return types likewise::

          >>> TypeDecls.parse_types(':rtype: Int')
          ([], 'Int', '')

        One-word types can be included in `:param:` markup::

          >>> TypeDecls.parse_types(':param String x: abc')
          ([('x', 'String')], None, '')
          >>> TypeDecls.parse_types(':param Seq[String] xs: abc')
          ([('xs', 'Seq[String]')], None, '')

        We extend the Sphinx conventions with type parameters::

          >>> TypeDecls.parse_types("""
          ...   :forall: T, U
          ...   :type x: Dict[T, U]""")
          ([('x', 'Dict[T, U]')], None, '[T, U]')

        .. note: TODO: handle multi-line types

        '''
        arg_types = (
            re.findall(':type\s+(\w+):\s+(.*)', txt, re.MULTILINE) +
            [(n, t) for (t, n) in
             re.findall(':param\s+(\S+)\s+(\w+):', txt, re.MULTILINE)])
        rtypes = re.findall(':rtype:\s+(.*)', txt, re.MULTILINE)
        foralls = re.findall(':forall:\s+(.*)', txt, re.MULTILINE)
        return (arg_types,
                rtypes[0] if rtypes else None,
                '[' + foralls[0] + ']' if foralls else '')

    def fun_sig(self, node):
        wr, body, doc = self._doc(node)

        arg_types, rtype, foralls = option_fold(doc,
                                                self.parse_types,
                                                (None, None, ''))

        wr('def %s%s(' % (node.name, foralls))
        self.visit_arguments(node.args, types=arg_types)
        rtypedecl = ': ' + rtype if rtype else ''
        wr(')%s = ' % rtypedecl)
        return rtype, body

    def add_arg_type(self, wr, expr, default, types):
        arg_type = self._arg_type(expr, default, types)
        if arg_type:
            wr(': ' + arg_type)

    def _arg_type(self, arg, default, types):
        '''
        .. note: TODO: log a warning when falling back to Any
        '''
        fallback = None if self._def_stack[-1:] == ['lambda'] else 'Any'
        return ((types.get(arg.id) if isinstance(arg, ast.Name) else
                 types.get(arg) if isinstance(arg, type('')) else None)
                or
                (self._literal_type(default) if default else None)
                or
                fallback)

    def _literal_type(self, expr):
        types1 = [t for (pat, t)
                 in [(ast.Num(n=None), 'Num'),
                     (ast.Str(s=None), 'String'),
                     (ast.Name(id='True', ctx=None), 'Boolean'),
                     (ast.Name(id='False', ctx=None), 'Boolean')]
                 if tmatch(expr, pat)]
        types2 = [('Double' if isinstance(expr.n, type(1.0)) else 'Int')
                  if t == 'Num' else t
                  for t in types1]
        return types2[0] if types2 else None

    def fun_body(self, body, rtype):
        try:
            self.filter_funbody()
        except ImplementationDetail:
            pass
        else:
            suite1, ret = self.split_ret(body)
            suite = (suite1 + [loc(ast.Expr(ret.value), ret)]
                     if ret and not rtype
                     else body)

            self._def_stack.append('FunctionDef')
            self._suite(suite)
            self._def_stack.pop()

    def split_ret(self, body):
        return ((body[:-1], body[-1]) if (len(body) > 0 and
                                          isinstance(body[-1], ast.Return) and
                                          body[-1].value)
                else (body, None))

    def lambda_expr(self, wr, node):
        self._def_stack.append('lambda')

        if node.args.defaults:
            wr('new { def apply(')
            self.visit_arguments(node.args)
            wr(') = ')
        else:
            wr('(')
            self.visit_arguments(node.args)
            wr(') => { ')

        self.visit(node.body)
        wr(' }')
        self._def_stack.pop()

    def typed_expr(self, wr, node_opt,
                   typed_id='typed'):
        '''type ascription

        TODO: work out details of importing typed

        :return: [unmatched node] or []
        '''
        for node in node_opt:
            if tmatch(node, ast.Call(func=ast.Name(id=typed_id, ctx=None),
                                     args=[None, ast.Str(s=None)],
                                     keywords=[], starargs=None,
                                     kwargs=None)):
                wr('(')
                self.visit(node.args[0])
                wr(': ' + node.args[1].s + ')')
                return []
        return node_opt


class ClassStructure(TypeDecls):
    def class_sig(self, node):
        wr, body, doc1 = self._doc(node)

        # skip 1st (self) arg in methods, including constructor
        ctors, body, arg_types, foralls = self._find_constructors(body, doc1)
        self._def_stack.append('ClassDef')
        wr('class %s%s(' % (node.name, foralls))
        for fd in ctors:
            self.visit_arguments(fd.args, types=arg_types)
        wr(')')

        if node.bases:
            notObject = [b for b in node.bases
                         if not (isinstance(b, ast.Name)
                                 and b.id == 'object')]
            if notObject:
                wr(' extends ')
                self._items(wr, notObject)  # TODO: test multiple bases
        wr(' ')
        return wr, ctors, body

    def class_body(self, wr, ctors, body,
                   this='self'):
        with self._block():
            wr('%s =>' % this)
            self.newline()

            for fd in ctors:
                con_body, _ = self.split_ret(fd.body)
                for stmt in con_body:
                    self.visit(stmt)

            for stmt in body:
                self.visit(stmt)

        self._def_stack.pop()

    def _find_constructors(self, suite, class_doc):
        ctor_pats = [
            ast.FunctionDef(name=name, args=None, body=None,
                            decorator_list=None)
            for name in ['__new__', '__init__']]

        def is_ctor(stmt):
            return [1 for pat in ctor_pats if tmatch(stmt, pat)]

        ctors, other = partition(suite, is_ctor)

        limitation(len(ctors) <= 1)

        # Combine constructor doc with class's doc for getting types.
        doc = '\n'.join(option_iter(class_doc)
                        + [s for fd in ctors
                           for s in option_iter(ast.get_docstring(fd))])
        arg_types, _, foralls = self.parse_types(doc)
        return ctors, other, arg_types, foralls

    def assign_field(self, targets):
        '''Translate self.x = ... to var x = ... .
        '''
        if (['ClassDef'] == self._def_stack[-1:] and
            len(targets) == 1 and
            tmatch(targets[0],
                   ast.Attribute(value=ast.Name(id='self', ctx=ast.Load()),
                                 attr=None, ctx=ast.Store()))):
            return [targets[0].attr]
        else:
            return []


class Assignment(object):
    def assign_targets(self, wr, node):
        targets = node.targets
        field1 = self.assign_field(targets)
        if field1:
            for field in field1:
                wr('var %s' % field)
            return node.value

        names = ([targets[0]] if (len(targets) == 1 and
                                  isinstance(targets[0], ast.Name))
                 else targets[0].elts if (
                         len(targets) == 1 and
                         isinstance(targets[0], ast.Tuple) and
                         not [not_name for not_name in targets[0].elts
                              if not isinstance(not_name, ast.Name)])
                 else [])

        if (len(names) > 0 and
            # if the first is 'var'...
            tmatch(names[0],
                   ast.Name(id='var', ctx=ast.Store()))):
            limitation(isinstance(node.value, ast.Tuple))
            limitation(len(node.value.elts) > 1)
            wr('var ')
            self._items(wr, names[1:], parens=len(names) > 2)
            return loc(ast.Tuple(elts=node.value.elts[1:]), node.value)

        if (len(names) > 0):
            wr('val ')
            self._items(wr, names, parens=len(names) > 1)
            return node.value

        self._items(wr, targets, parens=len(targets) > 1)
        return node.value


class ReRaise(object):
    def ex_wildcard(self, wr):
        wr('_ex')  # KLUDGE

    def reraise(self, wr):
        wr('throw _ex')  # KLUDGE


class Reify(object):
    '''Scala syntax for partially applied functions, class values.

    partially applied function KLUDGE
    class value KLUDGE
    '''
    def __init__(self, partial_app):
        self._partial_app = partial_app

    def reify(self, wr, node_opt):
        return self._reify_kwargs(
            wr, self._reify_class(
                wr, self._reify_func(wr, node_opt)))

    def _reify_kwargs(self, wr, node_opt,
                      dict_id='dict'):
        '''translate dict(x=1, y=2) to dict(x -> 1, y -> 2)

        and dict(a, x=y) as dict(a, x -> y)

        ..todo: consider applying this to any call, not just dict
        '''
        for node in node_opt:
            if (tmatch(node,
                       ast.Call(func=ast.Name(id=dict_id, ctx=None),
                                args=None, keywords=None, starargs=None,
                                kwargs=None))
                and node.keywords):
                limitation(len(node.args) <= 1)
                wr('dict(')
                if node.args:
                    self.visit(node.args[0])
                    wr(', ')
                for ix, kw in enumerate(node.keywords):
                    if ix > 0:
                        wr(', ')
                    wr('"%s"' % kw.arg)
                    wr(' -> ')
                    self.visit(kw.value)
                wr(')')
                return []
        return node_opt

    def _reify_func(self, wr, node_opt):
        for node in node_opt:
            if tmatch(node,
                      ast.Call(func=ast.Name(id=self._partial_app, ctx=None),
                               args=[None], keywords=[], starargs=None,
                               kwargs=None)):
                self.visit(node.args[0])
                wr(' _')
                return []
        return node_opt

    def _reify_class(self, wr, node_opt,
                     classof_id='classOf'):
        for node in node_opt:
            if tmatch(node, ast.Call(func=ast.Name(id=classof_id, ctx=None),
                                     args=[None, ast.Str(s=None)],
                                     keywords=[], starargs=None,
                                     kwargs=None)):
                wr('classOf[')
                self.visit(node.args[0])
                wr(']')
                return []
        return node_opt

    def skip_special_imports(self, node):
        '''skip: from functools import partial as pf_ (KLUDGE)

        TODO: skip from ..fp import typed
        '''
        return ([]
                if tmatch(node, ast.ImportFrom(
                        module='functools',
                        names=[ast.alias(name='partial',
                                         asname=self._partial_app)],
                        level=0))
                else [node])

    def adjust_class_call(self, wr, func):
        if self._is_class_ref(func):
            wr('new ')

    def _is_class_ref(self, expr):
        '''KLUDGE: distinguish f() from new F() by capitalization.
        '''
        names = [getName(expr)
                 for (cls, getName) in
                 [(ast.Name, lambda n: n.id),
                  (ast.Attribute, lambda n: n.attr)]
                 if isinstance(expr, cls)]
        return len([name for name in names if name[0].isupper()]) > 0


class PyToScala(ast.NodeVisitor,
                Reify, Assignment, ClassStructure, TypeDecls, ReRaise,
                APIFilter, PyRunTime,
                ModuleAttributes, LineSyntax):
    def __init__(self, modname, out, token_lines, find_package,
                 pkg=None, api=False,
                 partial_app='pf_', batteries_pfx='py',
                 py2scala='com.madmode.py2scala'):
        LineSyntax.__init__(self, out, token_lines)
        PyRunTime.__init__(self, find_package, batteries_pfx, py2scala)
        APIFilter.__init__(self, api)
        TypeDecls.__init__(self)
        Reify.__init__(self, partial_app)
        self._pkg = pkg
        self._modname = modname

    def visit_Module(self, node):
        '''Module(stmt* body)
        '''
        wr = self._out.write

        for pkg in option_iter(self._pkg):
            wr('package %s\n' % pkg)
            self.newline()

        for target in self.imports():
            wr('import %s\n' % target)

        _, body, _ = self._doc(node)
        wr('object %s ' % self._modname)

        with self._block():
            self.mod_attrs(wr, self._pkg, self._modname)

            for stmt in body:
                self.visit(stmt)

        self.newline()

    def _doc(self, node):
        self.newline()
        wr = self._sync(node)
        doc = ast.get_docstring(node)
        if doc:
            limitation('*/' not in doc)
            wr('/**')
            self.newline()
            for line in doc.split('\n'):
                wr(line)
                self.newline()
            wr('*/')
            self.newline()
            return wr, node.body[1:], doc
        return wr, node.body, doc

    def _suite(self, body):
        with self._block():
            for stmt in body:
                self.visit(stmt)

    def _items(self, wr, items, parens=False):
        if parens:
            wr('(')
        for ix, i in enumerate(items):
            if ix > 0:
                wr(', ')
            self.visit(i)
        if parens:
            wr(')')

    def _opt(self, wr, node):
        if node:
            wr(', ')
            self.visit(node)

    def visit_FunctionDef(self, node):
        '''FunctionDef(identifier name, arguments args,
                            stmt* body, expr* decorator_list)
        '''
        try:
            self.check_fun_name(node.name)
        except ImplementationDetail:
            pass
        else:
            self._decorators(node)
            rtype, body = self.fun_sig(node)
            self.fun_body(body, rtype)

    def _decorators(self, node):
        wr = self._sync(node)
        for expr in node.decorator_list:
            wr('@')
            self.visit(expr)
            self.newline()

    def visit_ClassDef(self, node):
        '''ClassDef(identifier name, expr* bases, stmt* body,
                    expr* decorator_list)

        .. note: TODO: test setting attributes in __new__.
        '''
        self._decorators(node)
        wr, ctors, body = self.class_sig(node)
        self.class_body(wr, ctors, body)

    def visit_Return(self, node):
        '''Return(expr? value)
        '''
        wr = self._sync(node)
        wr('return')
        if node.value:
            wr(' ')
            self.visit(node.value)
        self.newline()

    def visit_Delete(self, node):
        '''Delete(expr* targets)
        '''
        limitation(tmatch(node.targets,
                          [ast.Subscript(value=None,
                                         slice=ast.Index(value=None),
                                         ctx=ast.Del())]))
        wr = self._sync(node)
        target = node.targets[0]
        self.visit(target.value)
        wr('.__delitem__(')
        self.visit(target.slice.value)
        wr(')')
        self.newline()

    def visit_Assign(self, node):
        '''Assign(expr* targets, expr value)

        TODO: skip _xyz in API mode
        '''
        wr = self._sync(node)
        x = self.assign_targets(wr, node)
        wr(' = ')
        self.visit(x)
        self.newline()

    def visit_AugAssign(self, node):
        '''AugAssign(expr target, operator op, expr value)
        '''
        self.visit(node.target)
        wr = self._sync(node)
        wr(' ')
        wr(self._op(node.op))
        wr('= ')
        self.visit(node.value)
        self.newline()

    def visit_Print(self, node):
        '''Print(expr? dest, expr* values, bool nl)
        '''
        wr = self._sync(node)
        wr('print(')
        self._items(wr, option_iter(node.dest) + node.values)
        if node.nl:
            wr(r', "\n"')
        wr(')')
        self.newline()

    def visit_For(self, node):
        '''For(expr target, expr iter, stmt* body, stmt* orelse)

        Use local boolean to implement orelse.
        '''
        wr = self._sync(node)
        elsevar_ = ['any_iter%s' % id(s)
                    for s in [node.orelse] if node.orelse]

        for elsevar in elsevar_:
            wr('var %s = false' % elsevar)
            self.newline()

        wr('for (')
        self.visit(node.target)
        wr(' <- ')
        self.visit(node.iter)
        wr(') ')
        with self._block():
            for elsevar in elsevar_:
                wr('%s = true' % elsevar)
                self.newline()
            for stmt in node.body:
                self.visit(stmt)

        for elsevar in elsevar_:
            wr('if (! %s)' % elsevar)
            self._suite(node.orelse)

    def visit_While(self, node):
        '''While(expr test, stmt* body, stmt* orelse)
        '''
        limitation(not node.orelse)
        wr = self._sync(node)
        wr('while (')
        self.visit(node.test)
        wr(') ')
        self._suite(node.body)

    def visit_If(self, node):
        '''If(expr test, stmt* body, stmt* orelse)
        '''
        wr = self._sync(node)
        wr('if (')
        self.visit(node.test)
        wr(') ')
        self._suite(node.body)
        if node.orelse:
            suite = node.orelse
            wr(' else ')
            if len(suite) == 1 and isinstance(suite[0], ast.If):
                self.visit(suite[0])
            else:
                self._suite(suite)

    def visit_With(self, node):
        '''With(expr context_expr, expr? optional_vars, stmt* body)
        '''
        wr = self._sync(node)
        wr('with_ (')
        self.visit(node.context_expr)
        wr(') ')
        with self._block():
            wr('case ')
            if node.optional_vars:
                self.visit(node.optional_vars)
            else:
                wr('_it')
            wr(' => ')
            self._suite(node.body)

    def visit_Raise(self, node):
        '''Raise(expr? type, expr? inst, expr? tback)
        '''
        limitation(not node.tback)

        wr = self._sync(node)
        if node.type:
            if node.inst:  # deprecated form
                wr('throw new ')
                self.visit(node.type)
                wr('(')
                self.visit(node.inst)
                wr(')')
            else:
                wr('throw ')
                self.visit(node.type)
        else:
            self.reraise()
        self.newline()

    def visit_TryExcept(self, node):
        ''' TryExcept(stmt* body, excepthandler* handlers, stmt* orelse)

        excepthandler = ExceptHandler(expr? type, expr? name, stmt* body)
                        attributes (int lineno, int col_offset)
        '''
        wr = self._sync(node)
        wr('try ')
        self._suite(node.body)
        wr('catch ')
        with self._block():
            for excepthandler in node.handlers:
                wr('case ')
                if excepthandler.name:
                    self.visit(excepthandler.name)
                else:
                    self.ex_wildcard(wr)
                if excepthandler.type:
                    wr(': ')
                    self.visit(excepthandler.type)
                wr(' => ')
                self._suite(excepthandler.body)
            if node.orelse:
                wr('case _ =>')
                self._suite(node.orelse)

    def visit_TryFinally(self, node):
        ''' TryFinally(stmt* body, stmt* finalbody)
        '''
        wr = self._sync(node)
        wr('try ')
        self._suite(node.body)
        wr(' finally ')
        self._suite(node.finalbody)

    def visit_Assert(self, node):
        '''Assert(expr test, expr? msg)
        '''
        wr = self._sync(node)
        wr('assert(')
        self.visit(node.test)
        self._opt(wr, node.msg)
        wr(')')
        self.newline()

    def visit_Import(self, node):
        """Import(alias* names)"""
        wr = self._sync(node)
        for name in node.names:
            wr('import ')
            path = self.adjust_pkg_path(name.name)
            if name.asname:
                wr('%s.{ %s => %s }' % ('.'.join(path[:-1]),
                                        path[-1], name.asname))
            else:
                wr('.'.join(path))
            self.newline()

    def visit_ImportFrom(self, node):
        """ImportFrom(identifier? module, alias* names, int? level)"""
        wr = self._sync(node)
        limitation(node.module)

        for node in self.skip_special_imports(node):
            wr('import ')
            wr('.'.join(self.adjust_pkg_path(node.module, node.level)))
            wr('.')
            wr('{')
            self._items(wr, node.names)
            wr('}')
            self.newline()

    def visit_Global(self, node):
        '''Global(identifier* names)
        '''
        limitation(False)
        wr = self._sync(node)
        wr('/* global ')
        wr(', '.join(node.names))
        wr(' */')
        self.newline()

    def visit_Expr(self, node):
        self._sync(node)
        if tmatch(node.value, ast.Str(s=None)):
            pass  # Skip docstrings and other inert string exprs.
        else:
            self.visit(node.value)
            self.newline()

    def visit_Pass(self, node):
        wr = self._sync(node)
        wr('/* pass */')
        self.newline()

    def visit_Break(self, node):
        wr = self._sync(node)
        wr('break')
        self.newline()

    def visit_Continue(self, node):
        wr = self._sync(node)
        wr('continue')
        self.newline()

    def visit_BoolOp(self, node):
        '''BoolOp(boolop op, expr* values)
        boolop = And | Or
        '''
        wr = self._sync(node)
        sym = {ast.And: '&&', ast.Or: '||'}[node.op.__class__]
        sep = ''
        for expr in node.values:
            wr(sep)
            self.visit(expr)
            sep = ' ' + sym + ' '

    operator = {ast.Add: '+', ast.Sub: '-', ast.Mult: '*', ast.Div: '/',
                ast.Mod: '%', ast.Pow: '**',
                ast.LShift: '<<', ast.RShift: '>>',
                ast.BitOr: '|',
                #ast.BitXor: '@@',
                ast.BitAnd: '&',
                #ast.FloorDiv: '@@'
                }

    def _op(self, op):
        cls = op.__class__
        limitation(cls in self.operator)
        return self.operator[cls]

    def visit_BinOp(self, node):
        '''BinOp(expr left, operator op, expr right)
        operator = Add | Sub | Mult | Div | Mod | Pow | LShift
                 | RShift | BitOr | BitXor | BitAnd | FloorDiv
        '''
        wr = self._sync(node)
        wr('(')  # parens necessary?
        self.visit(node.left)
        wr(' ')
        wr(self._op(node.op))
        wr(' ')
        self.visit(node.right)
        wr(')')

    def visit_UnaryOp(self, node):
        '''UnaryOp(unaryop op, expr operand)
        unaryop = Invert | Not | UAdd | USub
        '''
        limitation(isinstance(node.op, ast.Not))

        wr = self._sync(node)
        wr('! ')
        self.visit(node.operand)

    def visit_Lambda(self, node):
        '''Lambda(arguments args, expr body)

        Use `{ def apply([[args]]) = [[body]] }`
        to handle defaults. Otherwise, use `([[args]]) => { [[body]] }`.

        .. note:: If only some args have defaults, others will get type Any
                  and we don't have a convention for overriding that.
        '''
        wr = self._sync(node)
        self.lambda_expr(wr, node)

    def visit_IfExp(self, node):
        '''IfExp(expr test, expr body, expr orelse)
        '''
        wr = self._sync(node)
        wr('if (')
        self.visit(node.test)
        wr(') { ')
        self.visit(node.body)
        wr(' } else { ')
        self.visit(node.orelse)
        wr(' }')

    def visit_Dict(self, node):
        wr = self._sync(node)
        wr('Dict(')
        for ix, (k, v) in enumerate(zip(node.keys, node.values)):
            if ix > 0:
                wr(', ')
            self.visit(k)
            wr(' -> ')
            self.visit(v)
        wr(')')

    def visit_ListComp(self, node):
        '''ListComp(expr elt, comprehension* generators)

        comprehension = (expr target, expr iter, expr* ifs)
        '''
        wr = self._sync(node)
        wr('(for (')
        first = True
        for gen in node.generators:
            if not first:
                wr('; ')
            else:
                first = False
            self.visit(gen.target)
            wr(' <- ')
            self.visit(gen.iter)
            if gen.ifs:
                limitation(len(gen.ifs) == 1)
                wr(' if ')
                self.visit(gen.ifs[0])

        wr(') yield ')
        self.visit(node.elt)
        wr(')')

    def visit_Yield(self, node):
        '''Yield(expr? value)
        '''
        wr = self._sync(node)
        wr('/* TODO */ yield_(')
        if node.value:
            self.visit(node.value)
        wr(')')

    def visit_Compare(self, node):
        '''Compare(expr left, cmpop* ops, expr* comparators)
        compop = Eq | NotEq | Lt | LtE | Gt | GtE | Is | IsNot | In | NotIn
        '''
        wr = self._sync(node)
        sep = ''
        left = node.left
        for op, expr in zip(node.ops, node.comparators):
            wr(sep)
            if op.__class__ in (ast.In, ast.NotIn):
                if op.__class__ == ast.NotIn:
                    wr('! ')
                self.visit(expr)
                # TODO: consider __contains__ and implicit mapping to PySeq
                wr('.contains(')
                self.visit(node.left)
                wr(')')
            else:
                sym = {ast.Eq: '==',
                       ast.NotEq: '!=',
                       ast.Lt: '<',
                       ast.LtE: '<=',
                       ast.Gt: '>',
                       ast.GtE: '>=',
                       ast.Is: 'eq',
                       ast.IsNot: '!='}[op.__class__]
                self.visit(left)
                wr(' ' + sym + ' ')
                self.visit(expr)
            left = expr
            sep = ' && '

    def visit_Call(self, node):
        '''Call(expr func, expr* args, keyword* keywords,
                expr? starargs, expr? kwargs)
           keyword = (identifier arg, expr value)'''

        wr = self._sync(node)

        for node in self.reify(wr, self.typed_expr(wr, [node])):
            self.adjust_class_call(wr, node.func)
            self.visit(node.func)
            wr('(')
            ax = len(node.args)
            self._items(wr, node.args)
            kx = 0
            if node.keywords:
                for kx, keyword in enumerate(node.keywords):
                    if ax + kx > 0:
                        wr(', ')
                    wr(keyword.arg)
                    wr('=')
                    self.visit(keyword.value)

            if node.starargs:
                if ax + kx > 0:
                    wr(', ')
                self.visit(node.starargs)
                wr(' : _*')
                ax += 1

            if node.kwargs:
                if ax + kx > 0:
                    wr(', ')
                wr('/* TODO kwargs using Dynamic? */ ')
                self.visit(node.kwargs)
            wr(')')

    def visit_Num(self, node):
        wr = self._sync(node)
        wr(str(node.n))

    def visit_Str(self, node):
        wr = self._sync(node)
        wr('"' + node.s.encode("string_escape").replace('"', '\\"') + '"')

    def visit_Attribute(self, node):
        '''Attribute(expr value, identifier attr, expr_context ctx)
        '''
        wr = self._sync(node)
        self.visit(node.value)
        wr('.')
        wr(fix_kw(node.attr))

    def visit_Subscript(self, node):
        '''Subscript(expr value, slice slice, expr_context ctx)
        '''
        wr = self._sync(node)
        self.visit(node.value)
        slice = node.slice
        sk = slice.__class__
        ctxk = node.ctx.__class__

        def lower0():
            if slice.lower:
                self.visit(slice.lower)
            else:
                wr('0')

        if sk == ast.Index and ctxk in (ast.Load, ast.Store):
            wr('(')
            self.visit(slice.value)
            wr(')')
        elif sk == ast.Index and ctxk == ast.Del:
            wr(' -= ')
            self.visit(slice.value)
        elif sk == ast.Slice and ctxk == ast.Load:
            limitation(not slice.step)
            if slice.upper:
                wr('.slice(')
                lower0()
                wr(', ')
                self.visit(slice.upper)
            elif slice.lower:
                wr('.drop(')
                self.visit(slice.lower)
            else:
                wr('.drop(0')
            wr(')')
        else:
            limitation(True)

    def visit_Name(self, node):
        # hmm... ctx
        wr = self._sync(node)
        wr(fix_kw(node.id))

    def visit_List(self, node):
        wr = self._sync(node)
        wr(self.list_maker)
        self._items(wr, node.elts, parens=True)

    def visit_Tuple(self, node):
        '''Tuple(expr* elts, expr_context ctx)
        '''
        wr = self._sync(node)
        self._items(wr, node.elts, parens=True)

    def visit_arguments(self, node, types=None):
        '''(expr* args, identifier? vararg,
            identifier? kwarg, expr* defaults)

        '''
        wr = self._sync(node)
        types = dict(types) if types else {}
        ix = 0

        def comma(ix):
            if ix > 0:
                wr(', ')

        # Skip 1st argument inside class def
        args = (node.args[1:] if ['ClassDef'] == self._def_stack[-1:]
                else node.args)

        default_ix = (len(args) - len(node.defaults))
        for ix, expr in enumerate(args):
            comma(ix)
            self.visit(expr)
            default = (node.defaults[ix - default_ix]
                       if ix >= default_ix else None)
            self.add_arg_type(wr, expr, default, types)

            if default:
                wr('=')
                self.visit(default)

        if node.vararg:
            comma(ix)
            wr(node.vararg)
            wr(': ')
            wr(self._arg_type(node.vararg, None, types))
            wr('*')
            ix += 1

        if node.kwarg:
            comma(ix)
            wr('/* TODO kwarg */ ' + node.kwarg + ': Dict[String, Any]')

    def visit_alias(self, node):
        '''alias = (identifier name, identifier? asname)
        '''
        wr = self._sync(node)
        if node.asname:
            wr('%s => %s ' % (node.name, node.asname))
        else:
            wr(node.name)

    def generic_visit(self, node):
        import pdb; pdb.set_trace()
        raise NotImplementedError('need visitor for: %s %s' %
                                  (node.__class__.__name__, node))


def fix_kw(n):
    return n + ('_' if n in ('match',) else '')


def tmatch(candidate, pattern):
    if pattern is None:
        return True
    if type(pattern) in (type(0), type(''), type(True)):
        return candidate == pattern
    if isinstance(pattern, type([])):
        if not isinstance(candidate, type([])):
            return False
        return not [1 for (c, p) in zip(candidate, pattern)
                    if not tmatch(c, p)]
    if not isinstance(candidate, type(pattern)):
        return False
    if not candidate._fields == pattern._fields:
        return False
    return not [n for n in pattern._fields
        if not tmatch(getattr(candidate, n), getattr(pattern, n))]


def limitation(t):
    if not t:
        import pdb; pdb.set_trace()
        raise NotImplementedError


def mk_find_package(find_module, path_split, sys_path):
    std_bases = [std_base
                 # datetime is with the C extensions
                 for m in ['string', 'os', 'datetime']
                 for (_, std_fn, _) in [find_module(m)]
                 for (std_base, _) in [path_split(std_fn)]
                 ]

    def find_package(mod_file, pkg_name, level=0):
        if pkg_name in ['sys', 'pkg_resources']:
            return True, False, [pkg_name]

        base, _ = path_split(mod_file)
        while level > 1:
            base, _ = path_split(base)
            level -= 1
        pkg_parts = pkg_name.split('.')
        _, pkg_path, _ = find_module(pkg_parts[0], [base] + sys_path[1:])
        pkg_dir, pkg_fn = path_split(pkg_path)
        return pkg_dir in std_bases, pkg_dir == base, pkg_parts

    return find_package


if __name__ == '__main__':
    def _with_caps(main):
        from imp import find_module
        from os import path as os_path
        from sys import argv, stdout, path as sys_path

        def open_arg(path):
            if path not in argv:
                raise IOError('not a CLI arg: %s' % path)
            return open(path)

        return main(argv=argv[:],
                    stdout=stdout,
                    open=open_arg,
                    find_package=mk_find_package(find_module,
                                                 os_path.split,
                                                 sys_path))

    _with_caps(main)
