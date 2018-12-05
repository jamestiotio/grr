#!/usr/bin/env python
"""Library for processing of artifacts.

This file contains non-GRR specific pieces of artifact processing and is
intended to end up as an independent library.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

import itertools
import logging
import re

from future.utils import string_types

from grr_response_core.lib import objectfilter
from grr_response_core.lib import utils


class Error(Exception):
  """Base exception."""


class ConditionError(Error):
  """An invalid artifact condition was specified."""


class ArtifactProcessingError(Error):
  """Unable to process artifact."""


class KnowledgeBaseInterpolationError(Error):
  """Unable to interpolate path using the Knowledge Base."""


class KnowledgeBaseUninitializedError(Error):
  """Attempt to process artifact without a valid Knowledge Base."""


class KnowledgeBaseAttributesMissingError(Error):
  """Knowledge Base is missing key attributes."""


INTERPOLATED_REGEX = re.compile(r"%%([^%]+?)%%")


def InterpolateListKbAttributes(input_list, knowledge_base, ignore_errors):
  interpolated_list = []
  for element in input_list:
    interpolated_list.extend(
        InterpolateKbAttributes(element, knowledge_base, ignore_errors))
  return interpolated_list


def InterpolateKbAttributes(pattern, knowledge_base, ignore_errors=False):
  """Interpolate all knowledgebase attributes in pattern.

  Args:
    pattern: A string with potential interpolation markers. For example:
      "/home/%%users.username%%/Downloads/"
    knowledge_base: The knowledge_base to interpolate parameters from.
    ignore_errors: Set this to true to log errors instead of raising.

  Yields:
    All unique strings generated by expanding the pattern.
  """
  # TODO(hanuszczak): This function should accept only `unicode` patterns.

  components = []
  offset = 0

  for match in INTERPOLATED_REGEX.finditer(pattern):
    components.append([pattern[offset:match.start()]])
    # Expand the attribute into the set of possibilities:
    alternatives = []

    try:
      if u"." in match.group(1):  # e.g. %%users.username%%
        base_name, attr_name = match.group(1).split(u".", 1)
        kb_value = knowledge_base.Get(base_name.lower())
        if not kb_value:
          raise AttributeError(base_name.lower())
        elif isinstance(kb_value, string_types):
          alternatives.append(kb_value)
        else:
          # Iterate over repeated fields (e.g. users)
          sub_attrs = []
          for value in kb_value:
            sub_attr = value.Get(attr_name)
            # Ignore empty results
            if sub_attr:
              sub_attrs.append(unicode(sub_attr))

          # If we got some results we use them. On Windows it is common for
          # users.temp to be defined for some users, but not all users.
          if sub_attrs:
            alternatives.extend(sub_attrs)
          else:
            # If there were no results we raise
            raise AttributeError(match.group(1).lower())
      else:
        kb_value = knowledge_base.Get(match.group(1).lower())
        if not kb_value:
          raise AttributeError(match.group(1).lower())
        elif isinstance(kb_value, string_types):
          alternatives.append(kb_value)
    except AttributeError as e:
      if ignore_errors:
        logging.info("Failed to interpolate %s with the knowledgebase. %s",
                     pattern, e)
        raise StopIteration
      else:
        raise KnowledgeBaseInterpolationError(
            "Failed to interpolate %s with the knowledgebase. %s" % (pattern,
                                                                     e))

    components.append(set(alternatives))
    offset = match.end()

  components.append([pattern[offset:]])

  # Now calculate the cartesian products of all these sets to form all strings.
  for vector in itertools.product(*components):
    # TODO(hanuszczak): This call should be removed once the signature of this
    # function is fixed.
    vector = map(utils.SmartUnicode, vector)
    yield u"".join(vector)


def GetWindowsEnvironmentVariablesMap(knowledge_base):
  """Return a dictionary of environment variables and their values.

  Implementation maps variables mentioned in
  https://en.wikipedia.org/wiki/Environment_variable#Windows to known
  KB definitions.

  Args:
    knowledge_base: A knowledgebase object.

  Returns:
    A dictionary built from a given knowledgebase object where keys are
    variables names and values are their values.
  """

  environ_vars = {}

  if knowledge_base.environ_path:
    environ_vars["path"] = knowledge_base.environ_path

  if knowledge_base.environ_temp:
    environ_vars["temp"] = knowledge_base.environ_temp

  if knowledge_base.environ_systemroot:
    environ_vars["systemroot"] = knowledge_base.environ_systemroot

  if knowledge_base.environ_windir:
    environ_vars["windir"] = knowledge_base.environ_windir

  if knowledge_base.environ_programfiles:
    environ_vars["programfiles"] = knowledge_base.environ_programfiles
    environ_vars["programw6432"] = knowledge_base.environ_programfiles

  if knowledge_base.environ_programfilesx86:
    environ_vars["programfiles(x86)"] = knowledge_base.environ_programfilesx86

  if knowledge_base.environ_systemdrive:
    environ_vars["systemdrive"] = knowledge_base.environ_systemdrive

  if knowledge_base.environ_allusersprofile:
    environ_vars["allusersprofile"] = knowledge_base.environ_allusersprofile
    environ_vars["programdata"] = knowledge_base.environ_allusersprofile

  if knowledge_base.environ_allusersappdata:
    environ_vars["allusersappdata"] = knowledge_base.environ_allusersappdata

  for user in knowledge_base.users:
    if user.appdata:
      environ_vars.setdefault("appdata", []).append(user.appdata)

    if user.localappdata:
      environ_vars.setdefault("localappdata", []).append(user.localappdata)

    if user.userdomain:
      environ_vars.setdefault("userdomain", []).append(user.userdomain)

    if user.userprofile:
      environ_vars.setdefault("userprofile", []).append(user.userprofile)

  return environ_vars


def ExpandWindowsEnvironmentVariables(data_string, knowledge_base):
  r"""Take a string and expand any windows environment variables.

  Args:
    data_string: A string, e.g. "%SystemRoot%\\LogFiles"
    knowledge_base: A knowledgebase object.

  Returns:
    A string with available environment variables expanded. If we can't expand
    we just return the string with the original variables.
  """
  win_environ_regex = re.compile(r"%([^%]+?)%")
  components = []
  offset = 0
  for match in win_environ_regex.finditer(data_string):
    components.append(data_string[offset:match.start()])

    # KB environment variables are prefixed with environ_.
    kb_value = getattr(knowledge_base, "environ_%s" % match.group(1).lower(),
                       None)
    if isinstance(kb_value, string_types) and kb_value:
      components.append(kb_value)
    else:
      # Failed to expand, leave the variable as it was.
      components.append("%%%s%%" % match.group(1))
    offset = match.end()
  components.append(data_string[offset:])  # Append the final chunk.
  return "".join(components)


def CheckCondition(condition, check_object):
  """Check if a condition matches an object.

  Args:
    condition: A string condition e.g. "os == 'Windows'"
    check_object: Object to validate, e.g. an rdf_client.KnowledgeBase()

  Returns:
    True or False depending on whether the condition matches.

  Raises:
    ConditionError: If condition is bad.
  """
  try:
    of = objectfilter.Parser(condition).Parse()
    compiled_filter = of.Compile(objectfilter.BaseFilterImplementation)
    return compiled_filter.Matches(check_object)
  except objectfilter.Error as e:
    raise ConditionError(e)


def ExpandWindowsUserEnvironmentVariables(data_string,
                                          knowledge_base,
                                          sid=None,
                                          username=None):
  r"""Take a string and expand windows user environment variables based.

  Args:
    data_string: A string, e.g. "%TEMP%\\LogFiles"
    knowledge_base: A knowledgebase object.
    sid: A Windows SID for a user to expand for.
    username: A Windows user name to expand for.

  Returns:
    A string with available environment variables expanded.
  """
  win_environ_regex = re.compile(r"%([^%]+?)%")
  components = []
  offset = 0
  for match in win_environ_regex.finditer(data_string):
    components.append(data_string[offset:match.start()])
    kb_user = knowledge_base.GetUser(sid=sid, username=username)
    kb_value = None
    if kb_user:
      kb_value = getattr(kb_user, match.group(1).lower(), None)
    if isinstance(kb_value, string_types) and kb_value:
      components.append(kb_value)
    else:
      components.append("%%%s%%" % match.group(1))
    offset = match.end()

  components.append(data_string[offset:])  # Append the final chunk.
  return "".join(components)
